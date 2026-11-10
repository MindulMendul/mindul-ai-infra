"""GitHub Projects v2 reader and narrowly-scoped triage writer."""
import json
import urllib.request

PROJECT_QUERY = '''query($owner:String!,$number:Int!,$cursor:String,$field:String!,$sizeField:String!){
  user(login:$owner){projectV2(number:$number){id
    field(name:$field){... on ProjectV2SingleSelectField{id options{id name}}}
    sizeField:field(name:$sizeField){... on ProjectV2SingleSelectField{id options{id name}}}
    items(first:100,after:$cursor){nodes{id isArchived
      status:fieldValueByName(name:$field){... on ProjectV2ItemFieldSingleSelectValue{name}}
      size:fieldValueByName(name:$sizeField){... on ProjectV2ItemFieldSingleSelectValue{name}}
      content{... on Issue{number title body url state repository{nameWithOwner} labels(first:100){nodes{name}}}}}
      pageInfo{hasNextPage endCursor}}}}}'''

UPDATE_FIELD = '''mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){
  updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,
    value:{singleSelectOptionId:$option}}){projectV2Item{id}}}'''


class Projects:
    def __init__(self, token, config):
        self.token=token;self.config=config;self.fields={};self.project_id=None

    def request(self, query, variables):
        req=urllib.request.Request('https://api.github.com/graphql',
            data=json.dumps({'query':query,'variables':variables}).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json',
                     'User-Agent':'mindul-paseo-projects/1.0'})
        with urllib.request.urlopen(req,timeout=25) as response:data=json.load(response)
        if data.get('errors'):
            raise RuntimeError('GitHub Projects query/mutation failed; check Projects write access')
        return data['data']

    def _rest(self, method, path, body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request('https://api.github.com'+path,data=data,method=method,
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                     'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28',
                     'User-Agent':'mindul-paseo-projects/1.0'})
        with urllib.request.urlopen(req,timeout=25) as response:
            raw=response.read()
        return json.loads(raw) if raw else None

    def scan(self):
        config=self.config;query=PROJECT_QUERY
        owner_kind=config.get('owner_kind','user')
        if owner_kind=='organization':query=query.replace('user(login:','organization(login:')
        if owner_kind not in ('user','organization'):raise ValueError('Invalid project owner kind')
        cursor=None;items=[];project_id=None;cursors=set();status_field=None;size_field=None
        while True:
            result=self.request(query,{'owner':config['owner'],'number':config['number'],
                'cursor':cursor,'field':config.get('status_field','Status'),
                'sizeField':config.get('size_field','Size')})
            owner=result.get(owner_kind);project=owner.get('projectV2') if owner else None
            if not project:raise RuntimeError('Configured project not visible')
            if project_id and project_id!=project['id']:raise RuntimeError('Project changed while reading')
            status_field=project.get('field');size_field=project.get('sizeField')
            self._validate_options(status_field,config.get('status_values',['Initial','Approve','Reject','Ready']))
            self._validate_options(size_field,['XS','S','M','L'])
            project_id=project['id'];self.fields={'status':status_field,'size':size_field}
            for item in project['items']['nodes']:
                if not item:continue
                content=item.get('content')
                if item.get('isArchived') or not content or content.get('repository',{}).get('nameWithOwner')!='MindulMendul/eniveil':continue
                number=content.get('number')
                if not isinstance(number,int) or isinstance(number,bool) or number<1:raise RuntimeError('Invalid issue number')
                items.append({'id':item['id'],'number':number,
                    'url':f'https://github.com/MindulMendul/eniveil/issues/{number}',
                    'status':(item.get('status') or {}).get('name',''),
                    'size':(item.get('size') or {}).get('name',''),
                    'title':content.get('title') or '', 'body':content.get('body') or '',
                    'labels':[label['name'] for label in (content.get('labels') or {}).get('nodes',[])],
                    'open':content.get('state')=='OPEN'})
            page=project['items']['pageInfo']
            if not page['hasNextPage']:
                self.project_id=project_id
                return project_id,items
            cursor=page['endCursor']
            if not cursor or cursor in cursors:raise RuntimeError('Invalid project pagination')
            cursors.add(cursor)

    @staticmethod
    def _validate_options(field, expected):
        if not field:raise RuntimeError('Configured Project single-select field not visible')
        options={option['name'].casefold() for option in field.get('options',[])}
        missing=[name for name in expected if name.casefold() not in options]
        if missing:raise RuntimeError('Missing Project options: '+', '.join(missing))

    def existing_labels(self):
        data=self._rest('GET','/repos/MindulMendul/eniveil/labels?per_page=100')
        return sorted({row['name'] for row in data if isinstance(row,dict) and isinstance(row.get('name'),str)})

    def set_field(self, item_id, kind, value):
        field=self.fields.get(kind)
        if not field or not self.project_id:raise RuntimeError('Projects fields were not loaded')
        option=next((o for o in field['options'] if o['name'].casefold()==value.casefold()),None)
        if not option:raise RuntimeError('Project option is not configured: '+value)
        self.request(UPDATE_FIELD,{'project':self.project_id,'item':item_id,
            'field':field['id'],'option':option['id']})

    def _comment_once(self, issue_number, marker, reason):
        comments=self._rest('GET',f'/repos/MindulMendul/eniveil/issues/{issue_number}/comments?per_page=100')
        if any(marker in item.get('body','') for item in comments if isinstance(item,dict)):return
        self._rest('POST',f'/repos/MindulMendul/eniveil/issues/{issue_number}/comments',
                   {'body':reason+'\n\n'+marker})

    def apply_triage(self, item_id, issue_number, delivery, result, approve_value, reject_value):
        marker='<!-- mindul-paseo-triage:'+delivery+' -->'
        if result['decision']=='reject':
            self._comment_once(issue_number,marker,result['reason'])
            self.set_field(item_id,'status',reject_value)
            return
        if result['decision']!='approve':raise ValueError('Invalid saved triage decision')
        allowed=set(self.existing_labels())
        if any(label not in allowed for label in result['labels']):
            raise RuntimeError('A selected issue label no longer exists')
        self._rest('PATCH',f'/repos/MindulMendul/eniveil/issues/{issue_number}',
                   {'title':result['title'],'body':result['body']})
        self._rest('POST',f'/repos/MindulMendul/eniveil/issues/{issue_number}/assignees',
                   {'assignees':['MindulMendul']})
        if result['labels']:
            self._rest('POST',f'/repos/MindulMendul/eniveil/issues/{issue_number}/labels',
                       {'labels':result['labels']})
        self.set_field(item_id,'size',result['size'])
        self.set_field(item_id,'status',approve_value)
