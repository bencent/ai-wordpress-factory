"""Versioned, allowlisted recovery evidence. No generated text or raw errors."""
from math import isfinite
from state import TaskStatus
from domain.failures import SAFE_RUN_ERROR_CODES

STAGES=frozenset({'planner','research','writer','critic','seo','quality_evaluator','router',
    'content_fixer','final_reviewer','frontend','frontend_security','greenlight_conversion',
    'frontend_validation','production_quality','image_preparation','preview','rendered_technical',
    'visual_quality','frontend_retry'})
EVENTS=frozenset({'workflow_started','agent_started','agent_completed','agent_failed',
    'checkpoint_produced','awaiting_approval_reached','workflow_completed','workflow_failed'})
GATES=('quality_result','frontend_security_result','frontend_validation_result',
       'frontend_production_quality_result','rendered_technical_result')


def checkpoint(value, *, task_id, workspace_id, run_id, images=None):
    if type(value) is not dict or value.get('id')!=task_id:
        raise ValueError('Checkpoint identity mismatch')
    result={'schema_version':1,'id':task_id,'workspace_id':workspace_id,'run_id':run_id}
    status=value.get('status')
    result['status']=status if status in {s.name for s in TaskStatus} else 'UNKNOWN'
    kind=value.get('content_type')
    result['content_type']=kind if kind in ('BLOG_POST','PAGE','POST') else 'UNKNOWN'
    stage=value.get('stage')
    result['stage']=stage if type(stage) is str and stage in STAGES else None
    completed=value.get('completed_stages',[])
    result['completed_stages']=list(dict.fromkeys(s for s in completed if type(s) is str and s in STAGES)) if type(completed) is list else []
    for key in GATES:
        data=value.get(key)
        if type(data) is not dict: continue
        safe={}
        if type(data.get('passed')) is bool: safe['passed']=data['passed']
        score=data.get('score')
        if type(score) in (int,float) and isfinite(score): safe['score']=score
        for name in ('issues','errors','warnings'):
            if type(data.get(name)) is list: safe[name+'_count']=len(data[name])
        # Preserve counts when the input is an already sanitized callback.
        for name in ('issues_count','errors_count','warnings_count'):
            if type(data.get(name)) is int and data[name]>=0: safe[name]=data[name]
        result[key]=safe
    visual=value.get('visual_quality_result')
    if type(visual) is dict and visual.get('action') in ('pass','warn','human_review'):
        result['visual_quality_result']={'action':visual['action']}
    for key in ('retry_count','frontend_retry_count'):
        if type(value.get(key)) is int and value[key]>=0: result[key]=value[key]
    code=value.get('error_code')
    if code in SAFE_RUN_ERROR_CODES: result['error_code']=code
    elif status in ('FAILED','FAILED_NEEDS_ATTENTION'):
        result['error_code']='FACTORY_VALIDATION_FAILED'
    gate=value.get('final_failed_gate')
    if gate in ('FRONTEND_SECURITY','FRONTEND_CONVERSION','FRONTEND_VALIDATION',
                'FRONTEND_PRODUCTION_QUALITY','RENDERED_TECHNICAL','PREVIEW_RENDER'):
        result['final_failed_gate']=gate
    if images is not None and type(value.get('image_artifact')) is dict:
        result['image_artifact']=images.persisted(value['image_artifact'])
    return result
