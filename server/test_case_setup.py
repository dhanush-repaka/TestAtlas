"""Shared generation contract for reproducible starting states and entry steps."""
import json

SETUP_SCHEMA = json.dumps({'status':'specified','starting_point':'documented application entry or API/library entry','initial_state':'session, fixture and exact starting data','steps':[{'action':'initial setup/login/navigation action','expected':'observable setup result'}]})
SETUP_RULES = '''START EVERY CASE FROM A REPRODUCIBLE BEGINNING.
Return a separate setup object (status, starting_point, initial_state, steps). Put initial entry, data preparation, required login and navigation in setup.steps, in execution order; put the actual scenario checks in steps. The server prepends setup steps to the saved test case.
For UI cases begin at the evidenced application entry/home/login page, establish the session when required, and navigate to the feature. Do not start midway through the UI or silently assume an open screen. Do not invent a login requirement, URL, menu or page that is absent from the evidence.
For API/library cases begin with explicit client/instance/fixture initialization and seed the starting data. Do not invent a UI login flow for a library. For quantities, balances and reservations state the exact initial values and trace every subsequent delta; the final expected value must be consistent with that initial state.
Do not perform the behaviour under test during setup. When login is itself the scenario (including failed login), setup opens the login entry and keeps the user signed out; the scenario performs the login attempt. Prefer an explicit test fixture for initial data instead of using the operation under test to arrange its own prerequisites.
Preconditions must be reproducible and compatible with the first action. Each setup step needs an observable expected result. Setup/navigation steps do not count as acceptance-criterion coverage or behavioural depth.
If the evidence does not establish the required entry or setup, return setup.status=needs_clarification and explain the missing information in initial_state. Such drafts are rejected with a setup clarification message; never invent navigation to make them look executable.'''


def add_initial_setup(case, raw):
    setup = raw.get('setup') if isinstance(raw,dict) else None
    if not isinstance(setup,dict) or setup.get('status') != 'specified':
        missing = setup.get('initial_state') if isinstance(setup,dict) else 'Starting point and setup steps were not supplied'
        raise ValueError(f'Setup needs clarification: {missing}')
    if not all(isinstance(setup.get(key),str) and setup[key].strip() for key in ('starting_point','initial_state')):
        raise ValueError('Setup needs clarification: starting point and initial state are required')
    raw_steps = setup.get('steps')
    if not isinstance(raw_steps,list) or not raw_steps:
        raise ValueError('Setup needs clarification: initial entry/setup steps are required')
    steps=[]
    for step in raw_steps:
        if not isinstance(step,dict) or not all(isinstance(step.get(k),str) and step[k].strip() for k in ('action','expected')):
            raise ValueError('Setup needs clarification: every setup step needs an action and expected result')
        steps.append({'action':step['action'].strip(),'expected':step['expected'].strip(),'kind':'setup'})
    result={**case,'steps':steps+case['steps']}
    result['preconditions']='Starting point: '+setup['starting_point'].strip()+'\nInitial state: '+setup['initial_state'].strip()+ ('\n'+case['preconditions'] if case.get('preconditions') else '')
    return result
