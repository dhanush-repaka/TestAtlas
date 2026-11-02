"""Complete generation-protocol setup used by model response fixtures."""
import copy

def setup():
    return {'status':'specified','starting_point':'Test application entry','initial_state':'Fresh test fixture with known starting data',
            'steps':[{'action':'Open the documented test application entry and prepare its fixture','expected':'The specified starting state is established'}]}

def with_setup_payload(payload):
    payload=copy.deepcopy(payload)
    if not isinstance(payload,dict): return payload
    cases=payload.get('test_cases')
    if isinstance(cases,list):
        for case in cases:
            if isinstance(case,dict): case.setdefault('setup',setup())
    if isinstance(payload.get('test_case'),dict): payload['test_case'].setdefault('setup',setup())
    return payload
