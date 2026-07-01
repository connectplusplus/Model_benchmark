# reason: AssertionError: {'decision': 'deny', 'matched': ['p2'], 'reason': 'deny_override'}

import ipaddress
from datetime import datetime


def authorize(subject: dict, action: str, resource: dict, policies: list[dict], context: dict) -> dict:
    def subject_matches(policy_subjects):
        if not policy_subjects:
            return False
        for s in policy_subjects:
            if s == '*':
                return True
            if s == subject.get('id'):
                return True
            if s.startswith('role:') and s.split(':', 1)[1] in (subject.get('roles') or []):
                return True
            if s.startswith('department:') and s.split(':', 1)[1] == subject.get('department'):
                return True
        return False

    def action_matches(policy_actions):
        if not policy_actions:
            return False
        for a in policy_actions:
            if a == action or a == '*':
                return True
            if a.endswith('*') and action.startswith(a[:-1]):
                return True
        return False

    def resource_matches(policy_resources):
        if not policy_resources:
            return False
        tags = resource.get('tags') or []
        for r in policy_resources:
            if r == '*':
                return True
            if r == resource.get('id'):
                return True
            if r.startswith('type:') and r.split(':', 1)[1] == resource.get('type'):
                return True
            if r.startswith('tag:') and r.split(':', 1)[1] in tags:
                return True
        return False

    def parse_ts(s):
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        return datetime.fromisoformat(s)

    def conditions_match(conds):
        if not conds:
            return True
        if 'ip_cidr' in conds:
            try:
                ip = ipaddress.ip_address(context.get('ip'))
                net = ipaddress.ip_network(conds['ip_cidr'], strict=False)
                if ip not in net:
                    return False
            except (ValueError, TypeError):
                return False
        if 'mfa' in conds:
            if bool(context.get('mfa')) != bool(conds['mfa']):
                return False
        if 'before' in conds:
            try:
                if parse_ts(context.get('now')) >= parse_ts(conds['before']):
                    return False
            except (ValueError, TypeError, AttributeError):
                return False
        if 'after' in conds:
            try:
                if parse_ts(context.get('now')) <= parse_ts(conds['after']):
                    return False
            except (ValueError, TypeError, AttributeError):
                return False
        if 'resource_owner' in conds:
            if conds['resource_owner']:
                if subject.get('id') != resource.get('owner'):
                    return False
        if 'risk_lte' in conds:
            risk = context.get('risk')
            if not isinstance(risk, (int, float)):
                return False
            if risk > conds['risk_lte']:
                return False
        return True

    allow_matched = []
    deny_matched = []

    for idx, policy in enumerate(policies):
        if not subject_matches(policy.get('subjects', [])):
            continue
        if not action_matches(policy.get('actions', [])):
            continue
        if not resource_matches(policy.get('resources', [])):
            continue
        if not conditions_match(policy.get('conditions')):
            continue
        entry = (idx, policy.get('id'))
        if policy.get('effect') == 'deny':
            deny_matched.append(entry)
        elif policy.get('effect') == 'allow':
            allow_matched.append(entry)

    if deny_matched:
        matched = [pid for _, pid in sorted(deny_matched)]
        return {'decision': 'deny', 'matched': matched, 'reason': 'deny_override'}
    if allow_matched:
        matched = [pid for _, pid in sorted(allow_matched)]
        return {'decision': 'allow', 'matched': matched, 'reason': 'allowed'}
    return {'decision': 'deny', 'matched': [], 'reason': 'default_deny'}
