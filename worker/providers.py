"""Worker-only provider composition and per-call audit. No retry/fallback."""
from time import perf_counter
from uuid import uuid4
from domain.ai_runtime import ProviderBundle, ProviderFailure, ErrorCode, TextResult, RuntimeOnly, classify_error
from domain.providers import Capability, AIInvocation, InvocationStatus, ProviderError, VerificationStatus
from domain.execution import LeaseLost
from providers.composition import provider_from_connection, EnvironmentCredentialResolver
from service.execution import now
from worker.claiming import LeaseService


class InvocationPersistenceFailed(RuntimeError):
    def __init__(self):
        super().__init__('AI_INVOCATION_PERSISTENCE_FAILED')


class ProviderSession:
    def __init__(self, store, lease, run, config, *, provider_factory=provider_from_connection,
                 credential_resolver=None):
        self.store,self.lease,self.run=store,lease,run
        self.failure=None
        self.service=LeaseService(store)
        self.service.assert_active(lease)
        with store.workspace_reader(lease.workspace_id) as repo:
            connection=repo.get_provider_connection(run.provider_connection_id)
        if connection is None:
            raise ProviderFailure(ErrorCode.UNAVAILABLE)
        if connection.provider_type not in ('OPENAI', 'GROQ'):
            raise ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)
        if connection.verification_status == VerificationStatus.FAILED:
            raise ProviderFailure(ErrorCode.UNAVAILABLE)
        required=[Capability.TEXT]
        if config.agents.get('image',{}).get('enabled',True):
            required.append(Capability.IMAGE)
        # The current Factory frontend workflow always performs visual review.
        required.append(Capability.VISUAL_QUALITY)
        if any(cap not in connection.capabilities for cap in required):
            raise ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)
        with store.transaction() as repo:
            repo.pin_provider_snapshot(lease,connection)
        self.connection=connection
        resolver=credential_resolver or EnvironmentCredentialResolver()
        built={}
        for capability in required:
            try:
                provider=provider_factory(connection,capability,resolver)
            except Exception as error:
                raise classify_error(error) from None
            built[capability]=provider
        self.bundle=ProviderBundle(RecordingText(self,built[Capability.TEXT]),
            RecordingImage(self,built[Capability.IMAGE]) if Capability.IMAGE in built else None,
            RecordingVisual(self,built[Capability.VISUAL_QUALITY]))

    def check(self):
        if self.failure is not None:
            raise self.failure
        self.service.assert_active(self.lease)
        with self.store.workspace_reader(self.lease.workspace_id) as repo:
            connection=repo.get_provider_connection(self.connection.provider_connection_id)
        if connection != self.connection or connection.verification_status == VerificationStatus.FAILED:
            self.failure=ProviderFailure(ErrorCode.UNAVAILABLE)
            raise self.failure
        if connection.provider_type not in ('OPENAI', 'GROQ'):
            self.failure=ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)
            raise self.failure

    def invoke(self, capability, operation, request):
        self.check()
        invocation_id=str(uuid4())
        started=now()
        timer=perf_counter()
        result=None
        failure=None
        try:
            result=operation(request)
            if capability==Capability.TEXT and not isinstance(result,TextResult):
                raise ProviderFailure(ErrorCode.INVALID_RESPONSE)
            if capability!=Capability.TEXT and not result.success:
                # Only consume the exact safe D3 classification envelope.
                codes={'AI provider: '+code.value:code for code in ErrorCode}
                raise ProviderFailure(codes.get(result.error,ErrorCode.UNKNOWN))
        except Exception as error:
            failure=classify_error(error)
        # Existing D1 enum uses RATE_LIMIT / INVALID_REQUEST; Run/Event retain exact D3 code.
        mapping={ErrorCode.RATE_LIMITED:ProviderError.RATE_LIMIT,
                 ErrorCode.UNSUPPORTED_CAPABILITY:ProviderError.INVALID_REQUEST}
        code=(mapping.get(failure.code) or ProviderError(failure.code.value)) if failure else None
        model=getattr(result,'model',None) if result is not None else None
        model=model if type(model) is str and model else self.connection.default_model
        metrics=result if failure is None else None
        try:
            record=AIInvocation(invocation_id=invocation_id,workspace_id=self.lease.workspace_id,
                task_id=self.lease.task_id,run_id=self.lease.run_id,
                provider_connection_id=self.connection.provider_connection_id,
                capability=capability,provider_type=self.connection.provider_type,model=model,
                input_tokens=getattr(metrics,"input_tokens",None),output_tokens=getattr(metrics,"output_tokens",None),
                total_tokens=getattr(metrics,"total_tokens",None),
                latency_ms=getattr(metrics,"latency_ms",None) if getattr(metrics,"latency_ms",None) is not None else int((perf_counter()-timer)*1000),
                status=InvocationStatus.FAILED if failure else InvocationStatus.SUCCEEDED,
                classified_error=code,created_at=started)
            with self.store.transaction() as repo:
                repo.append_invocation(record)
        except Exception:
            self.failure=InvocationPersistenceFailed()
            raise self.failure from None
        if failure is not None:
            self.failure=failure
            raise failure from None
        # Late audit is retained, but the actual result never returns to the Agent.
        try:
            self.check()
        except Exception as error:
            self.failure=error
            raise
        return result


class RecordingText(RuntimeOnly):
    def __init__(self, session, provider): self._session,self._provider=session,provider
    def complete(self, request): return self._session.invoke(Capability.TEXT,self._provider.complete,request)


class RecordingImage(RuntimeOnly):
    def __init__(self, session, provider): self._session,self._provider=session,provider
    def generate(self, request): return self._session.invoke(Capability.IMAGE,self._provider.generate,request)


class RecordingVisual(RuntimeOnly):
    def __init__(self, session, provider): self._session,self._provider=session,provider
    def review(self, request): return self._session.invoke(Capability.VISUAL_QUALITY,self._provider.review,request)


class GuardedObserver:
    def __init__(self, session, observer): self.session,self.observer=session,observer
    def on_event(self, event):
        self.session.check()
        self.observer.on_event(event)
