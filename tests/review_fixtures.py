"""Test clients submit the assessment version a real case page supplies."""
def bind_review_versions(client):
    post=client.post
    def versioned_post(url, *args, **kwargs):
        body=kwargs.get('json')
        if '/test-cases/' in str(url) and str(url).endswith('/review') and isinstance(body,dict) and 'assessment_version' not in body:
            base=str(url).rsplit('/test-cases/',1)[0]
            case_id=str(url).split('/test-cases/',1)[1].split('/')[0]
            cases=client.get(base+'/test-cases')
            if cases.status_code==200:
                case=next((c for c in cases.json() if c['id']==case_id),None)
                if case:
                    kwargs['json']={**body,'assessment_version':case['assessment']['version']}
        if '/story-requirements/' in str(url) and str(url).endswith('/confirm') and isinstance(body,dict) and 'assessment_version' not in body:
            from server import db
            from server.assessment_projection import requirement_version
            req_id=str(url).rsplit('/story-requirements/',1)[1].split('/')[0]
            req=db.get_story_requirement(req_id)
            if req:
                kwargs['json']={**body,'assessment_version':requirement_version(req,db.list_documents(req['repo_id']))}
        return post(url,*args,**kwargs)
    client.post=versioned_post
    return client
