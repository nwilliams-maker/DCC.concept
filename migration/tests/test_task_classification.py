import pytest

from migration.task_classification import is_cvs_kiosk_removal, dispatch_task_state


@pytest.mark.parametrize('official,details,expected', [
    ('New Ad', 'Instructions: remove kiosk', False),
    ('Photo', 'Kiosk removal campaign notes', False),
    ('Kiosk Install', 'Replaces a kiosk removal', False),
    ('Pull Down', 'Remove kiosk project', False),
    ('Remove Kiosk', 'New ad instructions', True),
    ('Kiosk Removal', '', True),
    (' REMOVE  KIOSK ', '', True),
    ('', 'Remove Kiosk', True),
    ('', 'Photo for kiosk removal campaign', False),
])
def test_only_actual_removal_types_enter_cvs_bucket(official, details, expected):
    assert is_cvs_kiosk_removal(task_type=official, native_details=details,
        container_type='TEAM', team_id='cvs', removal_team_ids=['cvs']) is expected


@pytest.mark.parametrize('container,team', [('WORKER','cvs'),('ORG','cvs'),('TEAM','regular')])
def test_removals_on_other_containers_or_teams_stay_out(container, team):
    assert not is_cvs_kiosk_removal(task_type='Remove Kiosk',
        container_type=container, team_id=team, removal_team_ids=['cvs'])


@pytest.mark.parametrize('fields,address,expected', [
    ([{'name':'State','value':'California'}], {}, 'CA'),
    ([{'key':'state','value':'AZ'}], {'state':'CA'}, 'AZ'),
    ([], {'state':'California'}, 'CA'),
    ([{'key':'state','value':'unknown'}], {'state':'CA'}, 'CA'),
    ([], {}, 'UNKNOWN'),
])
def test_state_custom_field_keeps_new_tasks_in_correct_pod(fields, address, expected):
    task = {'customFields':fields, 'destination':{'address':address}}
    assert dispatch_task_state(task, {'CALIFORNIA':'CA','ARIZONA':'AZ'}) == expected
