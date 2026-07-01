# reason: AssertionError: {'team-a': {'read'}, 'team-b': set(), 'team-c': {'deploy'}}

def effective_acl(entries: list[dict]) -> dict[str, set[str]]:
    allows: dict[str, set[str]] = {}
    denies: dict[str, set[str]] = {}
    for entry in entries:
        if not all(k in entry for k in ("principal", "action", "effect")):
            continue
        principal = entry["principal"]
        action = entry["action"]
        effect = entry["effect"]
        if effect == "allow":
            allows.setdefault(principal, set()).add(action)
        elif effect == "deny":
            denies.setdefault(principal, set()).add(action)
    result: dict[str, set[str]] = {}
    for principal, actions in allows.items():
        result[principal] = actions - denies.get(principal, set())
    return result
