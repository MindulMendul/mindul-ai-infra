"""Read-only GitHub Projects v2 adapter. No issue-label approximation."""
import json
import urllib.request

PROJECT_QUERY = '''query($owner:String!,$number:Int!,$cursor:String,$field:String!){
  user(login:$owner){projectV2(number:$number){id field(name:$field){... on ProjectV2SingleSelectField{name options{name}}} items(first:100,after:$cursor){
    nodes{id isArchived fieldValueByName(name:$field){... on ProjectV2ItemFieldSingleSelectValue{name}}
      content{... on Issue{number url state repository{nameWithOwner}}}}
    pageInfo{hasNextPage endCursor}}}}}'''


class Projects:
    def __init__(self, token, config):
        self.token=token;self.config=config

    def request(self, query, variables):
        req=urllib.request.Request('https://api.github.com/graphql',
            data=json.dumps({'query':query,'variables':variables}).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json',
                     'User-Agent':'mindul-paseo-projects/1.0'})
        with urllib.request.urlopen(req,timeout=20) as response:
            data=json.load(response)
        if data.get('errors'):
            raise RuntimeError('GitHub Projects query failed; check project visibility and read:project access')
        return data['data']

    def scan(self):
        config=self.config;query=PROJECT_QUERY
        owner_kind=config.get('owner_kind','user')
        if owner_kind=='organization':query=query.replace('user(login:','organization(login:')
        if owner_kind not in ('user','organization'):raise ValueError('Invalid project owner kind')
        cursor=None;items=[];project_id=None;cursors=set()
        while True:
            result=self.request(query,{'owner':config['owner'],'number':config['number'],
                                      'cursor':cursor,'field':config.get('status_field','Status')})
            owner=result.get(owner_kind)
            project=owner.get('projectV2') if owner else None
            if not project:raise RuntimeError('Configured project not visible')
            if project_id and project_id!=project['id']:raise RuntimeError('Project changed while reading')
            field=project.get('field')
            if not field or not any(o['name'].casefold()==config.get('ready_value','Ready').casefold() for o in field['options']):
                raise RuntimeError('Configured Status/Ready option not visible; refusing to change queue')
            project_id=project['id']
            for item in project['items']['nodes']:
                if not item:continue
                content=item.get('content')
                if item.get('isArchived') or not content or content.get('repository',{}).get('nameWithOwner')!='MindulMendul/eniveil':continue
                number=content.get('number')
                if not isinstance(number,int) or isinstance(number,bool) or number<1:raise RuntimeError('Invalid issue number')
                # Construct URLs, never trust event text as an agent instruction or URL.
                items.append({'id':item['id'],'number':number,'url':f'https://github.com/MindulMendul/eniveil/issues/{number}',
                              'status':(item.get('fieldValueByName') or {}).get('name',''),'open':content['state']=='OPEN'})
            page=project['items']['pageInfo']
            if not page['hasNextPage']:return project_id,items
            cursor=page['endCursor']
            if not cursor or cursor in cursors:raise RuntimeError('Invalid project pagination')
            cursors.add(cursor)
