"""Task-level classification; descriptions never override official types."""
import re


def dispatch_task_state(task, state_map):
    """Use the dispatch State field before the geocoder's address state."""
    valid = set(state_map.values())
    values = [f.get('value') for f in (task.get('customFields') or [])
              if isinstance(f, dict) and ('state' in
              (str(f.get('name') or '').strip().lower(), str(f.get('key') or '').strip().lower()))]
    values.append(((task.get('destination') or {}).get('address') or {}).get('state'))
    for value in values:
        normalized = str(value or '').strip().upper()
        normalized = state_map.get(normalized, normalized)
        if normalized in valid:
            return normalized
    return 'UNKNOWN'


def is_cvs_kiosk_removal(*, task_type, container_type, team_id, removal_team_ids, native_details=''):
    # Legacy tasks can use a standalone task label instead of a custom field.
    # An official type always wins; free-text descriptions never use substring matching.
    official_type = re.sub(r'\s+', ' ', str(task_type or native_details or '').strip().lower())
    return (str(container_type or '').upper() == 'TEAM'
            and team_id in set(removal_team_ids or [])
            and official_type in ('kiosk removal', 'remove kiosk'))
