"""Background-only Factory entry; approval and publication are separate slices."""
from copy import deepcopy
from contracts import ApprovalPolicy, ApprovalPolicyMode, ClientProfile, BrandProfile
from config import Config
from state import Task as LegacyTask, TaskStatus, ContentType as LegacyContentType
from main import AIWordPressFactory
from domain.contracts import Task, TaskRun, RunMode
from domain.execution import LeaseLost
from domain.observer import ObserverError
from service.execution import PersistingObserver, build_version, now
from worker.claiming import LeaseService
from worker.local_images import LocalImages

HUMAN = {'mode': 'REQUIRE_HUMAN_REVIEW'}
LEGACY_HUMAN = ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict()


class BackgroundFactory(AIWordPressFactory):
    def _get_agent(self, agent_type):
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
            raise ValueError('Approval policy changed during execution')
        return ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)

    def _prepare_image_artifact(self, task):
        if not self.runtime_config.agents.get('image', {}).get('enabled', True):
            return None
        artifact = self.images.prepare(task, self._get_agent)
        self.save_state()
        return artifact


def map_task(task):
    if task.approval_policy_snapshot != HUMAN:
        raise ValueError('AUTO_PUBLISH is unavailable in Phase 8.1')
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
        raise ValueError('Client policy is unavailable in Phase 8.1')
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


class FactoryAdapter:
    def __init__(self, store, config_resolver, image_root, *, factory_class=BackgroundFactory,
                 image_downloader=None):
        self.store, self.config_resolver, self.image_root = store, config_resolver, image_root
        if not issubclass(factory_class, BackgroundFactory):
            raise ValueError('BackgroundFactory is required')
        self.factory_class, self.image_downloader = factory_class, image_downloader

    def __call__(self, lease, cancelled):
        service = LeaseService(self.store)
        service.assert_active(lease)
        with self.store.reader() as repo:
            task, run = repo.get(Task, lease.task_id), repo.get(TaskRun, lease.run_id)
        if run.run_mode != RunMode.INITIAL or run.workflow_state is not None:
            raise ValueError('Checkpoint resume is unavailable in Phase 8.1')
        legacy = map_task(task)
        cfg = self.config_resolver(task.site_id)
        if not isinstance(cfg, Config):
            raise ValueError('Explicit runtime config is required')
        cfg = deepcopy(cfg)
        # Worker agents never need WordPress credentials.
        cfg.wordpress_url = cfg.wordpress_username = cfg.wordpress_password = cfg.wordpress_app_password = ''
        factory = self.factory_class.for_run(legacy, cfg)
        options = {} if self.image_downloader is None else {'downloader': self.image_downloader}
        images = LocalImages(self.image_root, task.site_id, task.task_id, run.run_id, **options)
        factory.images = images
        observer = PersistingObserver(self.store, lease, cancelled, images)
        try:
            factory.run_workflow(task.task_id, observer=observer)
        except ObserverError:
            if observer.error is not None:
                raise observer.error
            raise
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
        version = build_version(task, legacy, image_data)
        with self.store.transaction() as repo:
            accepted = repo.complete_content_version(lease, version,
                observer.snapshot(legacy.to_dict()), now())
        if not accepted:
            raise LeaseLost()
