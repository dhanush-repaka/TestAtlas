"""Regression tests for full-file dependency context and conservative evidence gates."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kg.dev_graph_builder import build_dev_graph
from kg.repo_parser import parse_repo
from kg.doc_gaps import gap_analysis_context
from kg.source_evidence import gather_source_evidence, enforce_context, render_sources
from kg.source_lookup import extract_function_source
from server import llm_story_coverage as stories
from server import test_case_reassessment as cases


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def graph(self, files):
        for path,text in files.items():
            file=self.root/path
            file.parent.mkdir(parents=True,exist_ok=True)
            file.write_text(text)
        return build_dev_graph(parse_repo(self.root))

    def test_follows_callers_helpers_and_keeps_globals_decorators(self):
        g=self.graph({'entry.py':'from service import reserve\ndef route(n):\n    return reserve(n)\n',
                      'service.py':'from policy import allowed\nLIMIT = 10\ndef reserve(n):\n    return allowed(n, LIMIT)\n',
                      'policy.py':'def allowed(n, limit):\n    return n <= limit\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertEqual({f['path'] for f in evidence.files},{'entry.py','service.py','policy.py'})
        self.assertIn('LIMIT = 10',render_sources(evidence))
        self.assertIn('return n <= limit',render_sources(evidence))
        self.assertTrue(evidence.manifest()['context_complete'])
        self.assertEqual(evidence.symbols['reserve']['start_line'],3)
        self.assertEqual(enforce_context('supported',{'context_sufficient':True,'missing_context':[]},evidence),'supported')

    def test_more_than_forty_functions_are_not_silently_omitted(self):
        text='\n'.join(f'def function_{n}():\n    return {n}\n' for n in range(60))
        g=self.graph({'large.py':text})
        evidence=gather_source_evidence(g,self.root,modules=['large'])
        self.assertIn('function_59',evidence)
        self.assertEqual(evidence.files[0]['text'],text)

    def test_missing_context_cannot_be_overridden_by_confident_model(self):
        g=self.graph({'service.py':'import remote_gateway\ndef charge():\n    return remote_gateway.charge()\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertIn('unresolved_import',{i['kind'] for i in evidence.limitations})
        self.assertEqual(enforce_context('supported',{'context_sufficient':True,'missing_context':[]},evidence),'unknown')
        self.assertEqual(enforce_context('contradicted',{'context_sufficient':True,'missing_context':[]},evidence),'unknown')

    def test_runtime_configuration_is_not_assumed(self):
        g=self.graph({'service.py':'import os\ndef allowed():\n    return os.getenv("ALLOW_ORDERS") == "yes"\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertIn('runtime_configuration',{i['kind'] for i in evidence.limitations})
        self.assertEqual(enforce_context('supported',{'context_sufficient':True,'missing_context':[]},evidence),'unknown')

    def test_literal_configuration_file_is_included(self):
        g=self.graph({'service.py':'import json\ndef limit():\n    return json.load(open("settings.json"))["limit"]\n','settings.json':'{"limit": 10}'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertIn('settings.json',{f['path'] for f in evidence.files})
        self.assertIn('"limit": 10',render_sources(evidence))

    def test_sensitive_configuration_is_excluded_and_blocks_verdict(self):
        g=self.graph({'service.py':'def config():\n    return open("settings.json").read()\n','settings.json':'{"password":"private-value"}'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertNotIn('private-value',render_sources(evidence))
        self.assertIn('sensitive_configuration',{i['kind'] for i in evidence.limitations})
        self.assertFalse(evidence.manifest()['context_complete'])

    def test_relative_import_recovery_handles_from_dot_import(self):
        g=self.graph({'app/__init__.py':'','app/service.py':'from . import policy\ndef allowed():\n    return policy.allow()\n','app/policy.py':'def allow():\n    return False\n'})
        evidence=gather_source_evidence(g,self.root,node_ids=['function:app.service:allowed'])
        self.assertIn('app/policy.py',{f['path'] for f in evidence.files})
        self.assertNotIn('unresolved_import',{i['kind'] for i in evidence.limitations})

    def test_budget_and_depth_boundaries_are_visible(self):
        g=self.graph({'a.py':'from b import b\ndef a():\n    return b()\n','b.py':'from c import c\ndef b():\n    return c()\n','c.py':'def c():\n    return False\n'})
        shallow=gather_source_evidence(g,self.root,modules=['a'],max_depth=0)
        self.assertIn('depth_limit',{i['kind'] for i in shallow.limitations})
        capped=gather_source_evidence(g,self.root,modules=['a'],max_files=1)
        self.assertIn('context_limit',{i['kind'] for i in capped.limitations})
        oversized=gather_source_evidence(g,self.root,modules=['a'],max_file_chars=10)
        self.assertIn('file_limit',{i['kind'] for i in oversized.limitations})
        self.assertFalse(oversized.manifest()['context_complete'])

    def test_lexical_recovery_finds_module_outside_initial_retrieval(self):
        g=self.graph({'web.py':'def homepage():\n    return "home"\n','refund.py':'def refund_payment():\n    return "refunded"\n'})
        evidence=gather_source_evidence(g,self.root,modules=['web'],query='refund payment')
        self.assertIn('refund.py',{f['path'] for f in evidence.files})

    def test_symlink_escape_is_not_read(self):
        g=self.graph({'service.py':'def service():\n    return True\n'})
        with tempfile.TemporaryDirectory() as outside:
            target=Path(outside)/'outside.py'; target.write_text('SENSITIVE_OUTSIDE = True')
            (self.root/'escape.py').symlink_to(target)
            g.add_node('file:escape',type='File',domain='escape',path='escape.py')
            evidence=gather_source_evidence(g,self.root,modules=['escape'])
            self.assertNotIn('SENSITIVE_OUTSIDE',render_sources(evidence))
            self.assertIn('unsafe_path',{i['kind'] for i in evidence.limitations})

    def test_typescript_arrow_and_class_method_have_source_bodies(self):
        g=self.graph({'policy.ts':'export const allowed = (n: number) => n <= 10;\nexport class Policy { check(n: number) { return allowed(n); } }'})
        evidence=gather_source_evidence(g,self.root,modules=['policy'])
        self.assertIn('n <= 10',evidence['allowed'])
        self.assertIn('return allowed(n)',evidence['Policy.check'])
        self.assertIn('policy.ts',render_sources(evidence))

    def test_model_declared_gap_also_blocks_definitive_finding(self):
        g=self.graph({'service.py':'def service():\n    return True\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertEqual(enforce_context('supported',{'context_sufficient':False,'missing_context':['Caller permissions']},evidence),'unknown')
        self.assertEqual(enforce_context('supported',{},evidence),'unknown')

    def test_story_judgment_receives_helper_and_persists_manifest(self):
        g=self.graph({'service.py':'from policy import allowed\ndef reserve(n):\n    return allowed(n)\n','policy.py':'def allowed(n):\n    return False\n'})
        doc={'id':'story','name':'Reservations','content':'Orders up to 10 items must be accepted.'}
        context=gap_analysis_context(g,[doc])
        prompts=[]
        def model(client,model,prompt,site):
            prompts.append(prompt)
            if site.endswith('decompose'):
                return {'requirements':[{'text':doc['content'],'type':'positive'}]}
            self.assertIn('policy.py',prompt)
            self.assertIn('return False',prompt)
            return {'status':'contradicted','covers':['allowed'],'rationale':'The helper rejects every order.',
                    'context_sufficient':True,'missing_context':[], 'code_citations':[{'name':'allowed','quote':'return False'}]}
        with mock.patch.object(stories,'is_configured',return_value=True), mock.patch.object(stories,'_call_json',side_effect=model), mock.patch('openai.OpenAI'), mock.patch.object(stories.embeddings,'build_module_index',side_effect=RuntimeError('disabled')):
            result=stories.generate_story_requirements(doc,context,g,self.root)
        self.assertEqual(result.requirements[0]['status'],'contradicted')
        self.assertTrue(result.requirements[0]['source_context']['files'])
        self.assertTrue(result.requirements[0]['source_context']['citations'])

    def test_incomplete_context_prevents_three_way_alignment(self):
        g=self.graph({'service.py':'import remote\ndef service():\n    return remote.check()\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        context=gap_analysis_context(g,[{'id':'story','name':'Story','content':'Allow requests.'}])
        story={'kind':'story','id':'story','quote':'Allow requests.'}
        code={'kind':'code','id':'service','quote':'return remote.check()'}
        raw={'context_sufficient':True,'missing_context':[],
             'requirement_alignment':{'status':'supported','citations':[story]},
             'code_evidence':{'status':'supported','citations':[code]},
             'implementation_alignment':{'status':'supported','citations':[story,code]}}
        report=cases.validate_report(raw,{'id':'case','title':'Allow requests'},context,evidence)
        self.assertEqual(report['outcome'],'needs_clarification')
        self.assertEqual(report['code_evidence']['status'],'unknown')
        self.assertTrue(report['source_context']['limitations'])

    def test_unresolved_injected_dependency_blocks_code_support(self):
        g=self.graph({'service.py':'def charge(gateway):\n    return gateway.charge()\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        self.assertIn('dynamic_dispatch',{i['kind'] for i in evidence.limitations})
        self.assertEqual(enforce_context('supported',{'context_sufficient':True,'missing_context':[]},evidence),'unknown')

    def test_bypass_caller_is_present_alongside_a_correct_guard(self):
        g=self.graph({'guard.py':'def allowed(n):\n    return n <= 10\n',
                      'route.py':'from guard import allowed\ndef submit(n, bypass=False):\n    return True if bypass else allowed(n)\n'})
        evidence=gather_source_evidence(g,self.root,modules=['guard'])
        prompt=render_sources(evidence)
        self.assertIn('return n <= 10',prompt)
        self.assertIn('True if bypass else allowed(n)',prompt)

    def test_story_context_survives_storage_and_human_confirmation(self):
        from server import db
        g=self.graph({'service.py':'def service():\n    return True\n'})
        evidence=gather_source_evidence(g,self.root,modules=['service'])
        with mock.patch.object(db,'DATA_DIR',self.root/'db'), mock.patch.object(db,'DB_PATH',self.root/'db/test.db'):
            db.init_db()
            repo=db.create_repo({'name':'test','source_type':'local','local_path':str(self.root)})
            doc=db.create_document(repo['id'],{'name':'Story','content':'Allow requests.'})
            db.replace_story_requirements(repo['id'],doc['id'],None,[{'text':'Allow requests','type':'positive','status':'unknown','covers':['service'],'source_context':evidence.manifest()}])
            requirement=db.list_story_requirements(repo['id'])[0]
            self.assertEqual(requirement['source_context']['files'][0]['path'],'service.py')
            db.confirm_story_requirement(requirement['id'],'supported')
            self.assertEqual(db.get_story_requirement(requirement['id'])['source_context'],requirement['source_context'])
            self.assertEqual(db.get_story_requirement(requirement['id'])['ai_status'],'unknown')
