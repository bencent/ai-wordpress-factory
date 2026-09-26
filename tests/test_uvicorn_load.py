"""Public Uvicorn Config load only: no server, port binding or background work."""
from unittest.mock import patch
import uvicorn

def test_uvicorn_loads_application_without_external_resources(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with patch('socket.socket.bind',side_effect=AssertionError('No socket binding')), \
         patch('socket.socket.connect',side_effect=AssertionError('No external connection')), \
         patch('openai.OpenAI',side_effect=AssertionError('No SDK')), \
         patch('providers.composition.EnvironmentCredentialResolver.resolve',side_effect=AssertionError('No credentials')), \
         patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('No Publisher')):
        config=uvicorn.Config('api.app:app',host='127.0.0.1',port=0,log_config=None,lifespan='off')
        config.load()
        assert config.loaded
        assert callable(config.loaded_app)
