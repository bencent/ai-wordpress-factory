"""Background-only Factory entry; approval and publication are separate slices."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import struct
from contracts import ApprovalPolicy, ApprovalPolicyMode, ClientProfile, BrandProfile
from config import Config
from state import Task as LegacyTask, TaskStatus, ContentType as LegacyContentType
from main import AIWordPressFactory
from domain.contracts import Task, TaskRun, RunMode
from domain.execution import LeaseLost
from domain.failures import UnsafeApprovalPolicyError, require_human_policy, check_policy_sources
from domain.preview import PreviewRecord, PreviewAsset, PreviewAssetKind, PreviewAssetMediaType
from service.checkpoints import checkpoint, STAGES
from worker.safe_logging import safe_factory_logs
from domain.observer import ObserverError
from service.execution import PersistingObserver, build_version, now
from worker.claiming import LeaseService
from worker.local_images import LocalImages
from worker.providers import ProviderSession, GuardedObserver
from providers.composition import provider_from_connection
from persistence.connection import PersistenceError
from persistence.codec import CodecError

HUMAN = {'mode': 'REQUIRE_HUMAN_REVIEW'}
LEGACY_HUMAN = ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict()


class BackgroundFactory(AIWordPressFactory):
    def _workflow_error(self, error):
        if isinstance(error,UnsafeApprovalPolicyError):
            raise error
        raise RuntimeError('Factory execution failed') from None

    def _event_snapshot(self, task):
        value=vars(task).copy()
        value['status']=task.status.name
        value['content_type']=task.content_type.name
        value['stage']=getattr(self,'_checkpoint_stage',None)
        value['completed_stages']=getattr(self,'_completed_stages',[])
        value['error_code']=getattr(self,'_checkpoint_error',None)
        return checkpoint(value,task_id=task.id,workspace_id=self.run_context.workspace_id,
                          run_id=self.run_context.run_id,images=getattr(self,'images',None))

    def _emit(self, event_type, task, stage=None):
        if event_type=='agent_failed': self._checkpoint_error='EXECUTOR_FAILED'
        if stage in STAGES:
            self._checkpoint_stage=stage
            if event_type=='agent_completed':
                self._completed_stages=list(dict.fromkeys(getattr(self,'_completed_stages',[])+[stage]))
        super()._emit(event_type,task,stage)

    def _get_agent(self, agent_type):
        check_policy_sources(self.runtime_config)
        for current in self.state.tasks.values(): check_policy_sources(current)
        if agent_type in ('publisher', 'learner'):
            raise RuntimeError('Publication is unavailable in Phase 8.1')
        return super()._get_agent(agent_type)

    def _continue_post_approval(self, *args, **kwargs):
        raise RuntimeError('Publication is unavailable in Phase 8.1')

    def submit_approval_decision(self, *args, **kwargs):
        raise RuntimeError('Approval decisions are unavailable in Phase 8.1')

    def _manual_review_checkpoint(self, *args, **kwargs):
        raise RuntimeError('Interactive review is unavailable in the Worker')

    def _resolve_approval_policy(self, task):
        if task.approval_policy != LEGACY_HUMAN:
            raise UnsafeApprovalPolicyError()
        return ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)

    def _prepare_image_artifact(self, task):
        if not self.runtime_config.agents.get('image', {}).get('enabled', True):
            return None
        artifact = self.images.prepare(task, self._get_agent)
        self.save_state()
        return artifact


def map_task(task):
    check_policy_sources(task.client_brand_snapshot)
    if task.approval_policy_snapshot != HUMAN:
        raise UnsafeApprovalPolicyError()
    request = task.request_snapshot
    for key, expected in {'site_id': task.site_id, 'content_type': task.content_type.value,
                          'topic': task.topic, 'brief': task.brief,
                          'brand_profile_id': task.brand_profile_id}.items():
        if request.get(key) != expected:
            raise ValueError('Request snapshot mismatch')
    snapshot = deepcopy(task.client_brand_snapshot)
    # Resolver snapshots use the existing contracts under client_profile / brand_profile.
    # Empty snapshots select explicit IDs and default brand constraints.
    if set(snapshot) - {'client_profile', 'brand_profile'}:
        raise ValueError('Unsupported profile snapshot shape')
    client_data = snapshot.get('client_profile', {})
    brand_data = snapshot.get('brand_profile', client_data.get('brand_profile') or {})
    if client_data.get('approval_policy') not in (None, HUMAN, LEGACY_HUMAN):
        raise UnsafeApprovalPolicyError()
    client_data['approval_policy'] = deepcopy(LEGACY_HUMAN)
    client = ClientProfile.from_dict(client_data)
    brand = BrandProfile.from_dict(brand_data)
    if client.client_id and client.client_id != (task.client_profile_id or task.site_id):
        raise ValueError('Client identity mismatch')
    if brand.brand_id and brand.brand_id != task.brand_profile_id:
        raise ValueError('Brand identity mismatch')
    client.client_id = task.client_profile_id or task.site_id
    brand.brand_id = task.brand_profile_id
    client.brand_profile, client.approval_policy = brand, ApprovalPolicy.from_dict(LEGACY_HUMAN)
    description = task.brief
    if task.target_audience:
        description += '\n目標讀者：' + task.target_audience
    if task.page_purpose:
        description += '\n頁面目的：' + task.page_purpose
    return LegacyTask(id=task.task_id, title=task.topic, description=description,
        content_type=LegacyContentType.PAGE if task.content_type.value == 'PAGE' else LegacyContentType.BLOG_POST,
        client_profile=client.to_dict(), approval_policy=deepcopy(LEGACY_HUMAN))


def _validate_png_metadata(data: bytes) -> tuple[int, int]:
    """Validate PNG signature and IHDR chunk, return (width, height).

    Raises:
        ValueError: if PNG signature or IHDR chunk is invalid
    """
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError("Invalid PNG signature")
    if len(data) < 24:
        raise ValueError("PNG too small for IHDR")
    length = struct.unpack('>I', data[8:12])[0]
    if length != 13:
        raise ValueError(f"IHDR chunk length {length} != 13")
    if data[12:16] != b'IHDR':
        raise ValueError("First chunk is not IHDR")
    width = struct.unpack('>I', data[16:20])[0]
    height = struct.unpack('>I', data[20:24])[0]
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return width, height


def _build_preview_assets(
    preview_artifact,  # PreviewArtifact from contracts
    preview_base_dir: str,
) -> tuple[PreviewAsset, ...]:
    """Validate screenshots and build PreviewAsset tuple.

    Args:
        preview_artifact: PreviewArtifact with screenshot paths
        preview_base_dir: configured preview base directory (trusted root)

    Returns:
        Tuple of 4 PreviewAsset objects in deterministic kind order.

    Raises:
        ValueError: safe message, no paths, no raw exceptions
    """
    trusted_root = Path(preview_base_dir).resolve()
    assets = []
    kind_path_pairs = [
        (PreviewAssetKind.DESKTOP_VIEWPORT, preview_artifact.desktop_viewport_screenshot_path),
        (PreviewAssetKind.DESKTOP_FULL_PAGE, preview_artifact.desktop_full_page_screenshot_path),
        (PreviewAssetKind.MOBILE_VIEWPORT, preview_artifact.mobile_viewport_screenshot_path),
        (PreviewAssetKind.MOBILE_FULL_PAGE, preview_artifact.mobile_full_page_screenshot_path),
    ]
    for kind, abs_path in kind_path_pairs:
        path = Path(abs_path).resolve(strict=True)
        if not path.is_relative_to(trusted_root):
            raise ValueError("Screenshot outside artifact root")
        if not path.is_file() or path.is_symlink():
            raise ValueError("Screenshot is not a regular file")
        data = path.read_bytes()
        width, height = _validate_png_metadata(data)
        sha256_hash = sha256(data).hexdigest()
        byte_size = len(data)
        artifact_key = "previews/" + path.relative_to(trusted_root).as_posix()
        assets.append(PreviewAsset(
            preview_id=preview_artifact.preview_id,
            kind=kind,
            artifact_key=artifact_key,
            sha256=sha256_hash,
            media_type=PreviewAssetMediaType.PNG,
            width=width,
            height=height,
            byte_size=byte_size,
        ))
    return tuple(assets)


class FactoryAdapter:
    def __init__(self, store, config_resolver, image_root, *, factory_class=BackgroundFactory,
                 image_downloader=None, provider_factory=provider_from_connection, credential_resolver=None):
        self.provider_factory, self.credential_resolver = provider_factory, credential_resolver
        self.store, self.config_resolver, self.image_root = store, config_resolver, image_root
        if not issubclass(factory_class, BackgroundFactory):
            raise ValueError('BackgroundFactory is required')
        self.factory_class, self.image_downloader = factory_class, image_downloader

    def __call__(self, lease, cancelled):
        service = LeaseService(self.store)
        service.assert_active(lease)
        with self.store.workspace_reader(lease.workspace_id) as repo:
            task, run = repo.get_task(lease.task_id), repo.get_run(lease.task_id,lease.run_id)
        if task is None or run is None:
            raise LeaseLost()
        if run.run_mode != RunMode.INITIAL or run.workflow_state is not None:
            raise ValueError('Checkpoint resume is unavailable in Phase 8.1')
        legacy = map_task(task)
        cfg = self.config_resolver(task.site_id)
        if not isinstance(cfg, Config):
            raise ValueError('Explicit runtime config is required')
        check_policy_sources(cfg)
        check_policy_sources(legacy)
        cfg = deepcopy(cfg)
        # Worker agents never need WordPress credentials.
        cfg.wordpress_url = cfg.wordpress_username = cfg.wordpress_password = cfg.wordpress_app_password = ''
        session = ProviderSession(self.store,lease,run,cfg,provider_factory=self.provider_factory,
                                  credential_resolver=self.credential_resolver)
        cfg.openai_api_key = cfg.search_api_key = None
        factory = self.factory_class.for_run(legacy, cfg,providers=session.bundle,
                                            workspace_id=lease.workspace_id,run_id=lease.run_id)
        options = {} if self.image_downloader is None else {'downloader': self.image_downloader}
        images = LocalImages(self.image_root, task.site_id, task.task_id, run.run_id, workspace_id=lease.workspace_id, **options)
        factory.images = images
        observer = PersistingObserver(self.store, lease, cancelled, images)
        try:
            with safe_factory_logs():
                factory.run_workflow(task.task_id, observer=GuardedObserver(session,observer))
        except ObserverError:
            if session.failure is not None:
                raise session.failure
            if isinstance(observer.error, LeaseLost):
                raise observer.error
            if isinstance(observer.error, (PersistenceError, CodecError)):
                raise
            if observer.error is not None:
                raise observer.error
            raise
        session.check()
        if cancelled.is_set():
            raise LeaseLost()
        legacy = factory.state.get_task(task.task_id)
        if legacy.status != TaskStatus.AWAITING_APPROVAL:
            with self.store.transaction() as repo:
                accepted = repo.fail_run(lease, now(), 'FACTORY_VALIDATION_FAILED')
            if not accepted:
                raise LeaseLost()
            return
        image_data = None
        if cfg.agents.get('image', {}).get('enabled', True):
            from contracts import ImageArtifact
            artifact = ImageArtifact.from_dict(legacy.image_artifact)
            if not images.usable(artifact):
                raise ValueError('Image is not usable')
            image_data = images.persisted(artifact.to_dict())
        check_policy_sources(cfg)
        check_policy_sources(legacy)
        check_policy_sources(factory.runtime_config)
        version = build_version(task, legacy, image_data)
        # Persist preview if available
        from contracts import PreviewArtifact
        preview_artifact = None
        if legacy.preview_history:
            # Use the latest preview artifact
            preview_artifact = PreviewArtifact.from_dict(legacy.preview_history[-1])
        
        with self.store.transaction() as repo:
            accepted = repo.complete_content_version(lease, version,
                observer.snapshot(legacy.to_dict()), now())
            if not accepted:
                raise LeaseLost()
            
            if preview_artifact is not None:
                preview_base_dir = getattr(cfg, 'preview_base_dir', 'artifacts/previews')
                record = PreviewRecord(
                    preview_id=preview_artifact.preview_id,
                    workspace_id=lease.workspace_id,
                    task_id=lease.task_id,
                    run_id=lease.run_id,
                    content_version_id=version.content_version_id,
                    created_at=preview_artifact.created_at,
                )
                assets = _build_preview_assets(preview_artifact, preview_base_dir)
                repo.append_preview(record, assets)
        if not accepted:
            raise LeaseLost()
