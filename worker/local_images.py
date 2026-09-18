"""Run-local image materialization. No WordPress media IDs or uploads."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import requests
from contracts import ImageArtifact, ImageArtifactStatus, create_image_artifact

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def download_image(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Image provider returned an invalid URL')
    # URL comes only from the configured image provider, never a submission field.
    with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError('Image download failed')
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError('Image too large')
    return bytes(data)


class LocalImages:
    def __init__(self, root, site_id, task_id, run_id, *, downloader=download_image):
        # IDs become hashes rather than user-controlled filesystem path segments.
        self.root = Path(root).resolve()
        self.site_id, self.task_id, self.run_id = site_id, task_id, run_id
        self.downloader = downloader
        self.namespace = Path(sha256(site_id.encode()).hexdigest()[:16]) / sha256(task_id.encode()).hexdigest()[:32]

    def persisted(self, artifact):
        value = deepcopy(artifact)
        path = value.get('local_path')
        if path:
            path = Path(path)
            if path.is_absolute():
                value['local_path'] = path.resolve().relative_to(self.root).as_posix()
        # Provider URLs can expire and may contain signed tokens. Local file is the source.
        value['source_url'] = None
        return value

    def usable(self, artifact):
        try:
            if (artifact.status != ImageArtifactStatus.READY or not artifact.artifact_id
                    or artifact.wordpress_media_id is not None or artifact.wordpress_media_url is not None
                    or artifact.metadata.get('site_id') != self.site_id
                    or artifact.metadata.get('task_id') != self.task_id):
                return False
            path = Path(artifact.local_path)
            path = (path if path.is_absolute() else self.root / path).resolve()
            if not path.is_relative_to((self.root / self.namespace).resolve()):
                return False
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
                return False
            if sha256(path.read_bytes()).hexdigest() != artifact.metadata.get('checksum'):
                return False
            artifact.local_path = str(path)
            artifact.source_url = None
            return True
        except (TypeError, ValueError, OSError):
            return False

    def prepare(self, task, agent_factory):
        if task.image_artifact is not None:
            try:
                artifact = ImageArtifact.from_dict(task.image_artifact)
                if artifact.status != ImageArtifactStatus.READY:
                    return artifact
                if self.usable(artifact):
                    task.image_artifact = artifact.to_dict()
                    return artifact
            except (TypeError, ValueError, KeyError, AttributeError):
                pass
            artifact = create_image_artifact(status=ImageArtifactStatus.FAILED,
                metadata={'code': 'INVALID_LOCAL_IMAGE'})
        else:
            try:
                agent = agent_factory('image')
                prompt = agent._build_image_prompt(task.title, task.final_content or '')
                result = agent.provider.generate(agent._build_request(task, prompt))
                if not result.success or not result.image_url:
                    raise ValueError('Image generation failed')
                data = self.downloader(result.image_url)
                if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
                    raise ValueError('Invalid image bytes')
                if data.startswith(b'\x89PNG\r\n\x1a\n'):
                    suffix, mime = '.png', 'image/png'
                elif data.startswith(b'\xff\xd8\xff'):
                    suffix, mime = '.jpg', 'image/jpeg'
                elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
                    suffix, mime = '.webp', 'image/webp'
                else:
                    raise ValueError('Unsupported image format')
                artifact_id = str(uuid4())
                destination = self.root / self.namespace / (artifact_id + suffix)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.resolve().is_relative_to(self.root):
                    raise ValueError('Invalid image directory')
                with destination.open('xb') as handle:
                    handle.write(data)
                artifact = create_image_artifact(artifact_id=artifact_id,
                    status=ImageArtifactStatus.READY, local_path=str(destination), prompt=prompt,
                    provider=result.provider, model=result.model, content_type=mime,
                    width=result.width, height=result.height,
                    metadata={'site_id': self.site_id, 'task_id': self.task_id,
                              'run_id': self.run_id, 'checksum': sha256(data).hexdigest()})
            except Exception:
                artifact = create_image_artifact(status=ImageArtifactStatus.FAILED,
                    metadata={'code': 'LOCAL_IMAGE_PREPARATION_FAILED'})
        task.image_artifact = artifact.to_dict()
        task.image_status = 'success' if artifact.status == ImageArtifactStatus.READY else 'failed'
        return artifact
