"""An allowlisted credential-free projection; arbitrary Config objects never reach agents."""
from types import SimpleNamespace


def agent_settings(config):
    # Copy primitive options only, never Config.__post_init__, provider/client or unknown attributes.
    defaults={'ai_model':'gpt-4','ai_temperature':0.7,'ai_max_tokens':2000,'max_retries':3,
              'image_required':False,'allowed_domains':[],'frontend_max_html_size':102400,
              'frontend_max_css_size':51200,'frontend_max_js_size':51200,'frontend_max_total_size':204800}
    values={}
    for name,default in defaults.items():
        value=getattr(config,name,default)
        if type(value) is type(default):
            values[name]=([v for v in value if type(v) is str] if type(value) is list else value)
        else:
            values[name]=default
    enabled=getattr(config,'agents',{})
    values['agents']={name:{'enabled':bool(part.get('enabled',True))}
        for name,part in enabled.items() if type(name) is str and type(part) is dict} if type(enabled) is dict else {}
    return SimpleNamespace(**values)
