"""Who may do what.

Identity comes from the federation (CILogon, which also carries eduGAIN
identity providers); this module only decides what a signed-in person may
change.  Everything is readable by any signed-in user, since the result is
published to the world anyway.

A grant gives a *principal* (a person or a team) a *role* on a *scope*:

    scope      one object ("mesh #12") or every object of a kind ("all tests")
    role       owner        change it, delete it, decide who else has access
               editor       change it
               contributor  host groups only: add and remove hosts
               creator      kind-wide only: create new objects of that kind
                            (the creator becomes the owner of what they make)

Service administrators (User.is_admin) can do everything, including managing
people and teams.  A team manager can add and remove that team's members.
"""
from . import models as m

SCOPES = {"host": "Hosts", "hostgroup": "Host groups", "test": "Tests",
          "archive": "Archives", "mesh": "Meshes"}

ROLES = {
    "owner": {"edit", "members", "delete", "grant"},
    "editor": {"edit", "members"},
    "contributor": {"members"},
    "creator": {"create"},
}
ROLE_HELP = {
    "owner": "change, delete, and manage access",
    "editor": "change",
    "contributor": "add and remove hosts",
    "creator": "create new ones",
}


def roles_for(scope_type, kind_wide):
    """Roles that make sense for a scope, most powerful first."""
    roles = ["owner", "editor"]
    if scope_type == "hostgroup":
        roles.append("contributor")
    if kind_wide:
        roles.append("creator")
    return roles


class Access:
    """The permissions of one user, loaded once per request."""

    def __init__(self, session, user):
        self.session, self.user = session, user
        self._grants = None

    def principals(self):
        if self.user is None:
            return []
        return [("user", self.user.id)] + [("team", tm.team_id) for tm in self.user.memberships]

    def grants(self):
        if self._grants is None:
            self._grants = []
            for ptype, pid in self.principals():
                self._grants += (self.session.query(m.Grant)
                                 .filter_by(principal_type=ptype, principal_id=pid).all())
        return self._grants

    def can(self, action, scope_type, obj=None):
        user = self.user
        if user is None or not user.active:
            return False
        if action == "view" or user.is_admin:
            return True
        for g in self.grants():
            if g.scope_type != scope_type:
                continue
            if g.scope_id is not None and (obj is None or g.scope_id != getattr(obj, "id", None)):
                continue
            allowed = ROLES.get(g.role, set())
            if action in allowed:
                return True
            # Owning every object of a kind includes creating more of them.
            if action == "create" and g.scope_id is None and g.role == "owner":
                return True
        return False

    def can_manage_team(self, team):
        if self.user is None:
            return False
        return self.user.is_admin or any(tm.team_id == team.id and tm.is_manager
                                         for tm in self.user.memberships)


def grant(session, principal, role, scope_type, obj=None, by=None):
    """Add a grant unless an identical one exists.  Returns the Grant."""
    ptype = "team" if isinstance(principal, m.Team) else "user"
    scope_id = getattr(obj, "id", None)
    existing = (session.query(m.Grant)
                .filter_by(principal_type=ptype, principal_id=principal.id, role=role,
                           scope_type=scope_type, scope_id=scope_id).one_or_none())
    if existing:
        return existing
    g = m.Grant(principal_type=ptype, principal_id=principal.id, role=role,
                scope_type=scope_type, scope_id=scope_id, created_by=getattr(by, "label", by))
    session.add(g)
    return g


def grants_on(session, scope_type, obj=None):
    """Grants that apply to an object: its own, then the kind-wide ones."""
    q = session.query(m.Grant).filter_by(scope_type=scope_type)
    rows = [g for g in q if g.scope_id is None or (obj is not None and g.scope_id == obj.id)]
    users = {u.id: u for u in session.query(m.User)}
    teams = {t.id: t for t in session.query(m.Team)}
    out = []
    for g in sorted(rows, key=lambda g: (g.scope_id is None, g.role, g.principal_type)):
        p = (teams if g.principal_type == "team" else users).get(g.principal_id)
        if p is not None:
            out.append((g, p))
    return out


def merge_users(session, source, target):
    """Move a placeholder account's grants and team memberships to a real one."""
    for g in session.query(m.Grant).filter_by(principal_type="user", principal_id=source.id).all():
        scope = None
        if g.scope_id is not None:
            scope = session.get(m.SCOPE_MODELS[g.scope_type], g.scope_id)
        if g.scope_id is None or scope is not None:
            grant(session, target, g.role, g.scope_type, scope, by="merge")
        session.delete(g)
    have = {tm.team_id for tm in target.memberships}
    for tm in list(source.memberships):
        if tm.team_id not in have:
            target.memberships.append(m.TeamMember(team_id=tm.team_id, is_manager=tm.is_manager))
    target.is_admin = target.is_admin or source.is_admin
    # Logins that led to the absorbed account now lead to the target.
    subjects = [(i.subject, i.idp) for i in source.identities]
    if not source.is_placeholder:
        subjects.append((source.subject, source.idp))
    if target.is_placeholder and subjects:
        # The target was only a placeholder: it takes over the real login.
        (target.subject, target.idp), subjects = subjects[0], subjects[1:]
        target.name, target.email = target.name or source.name, target.email or source.email
        target.last_login = source.last_login
    session.flush()
    session.delete(source)
    session.flush()
    for subject, idp in subjects:
        session.add(m.UserIdentity(subject=subject, user_id=target.id, idp=idp))


def _principal_active(session, ptype, pid):
    if ptype == "user":
        u = session.get(m.User, pid)
        return u is not None and u.active
    t = session.get(m.Team, pid)
    return t is not None and any(tm.user.active for tm in t.members)


def only_changer_of(session, user):
    """[(grant, object)] for items that nobody but this person can change.

    Service administrators can always change everything, so these are the items
    that would be left to administrators alone if the person went away.
    """
    out, seen = [], set()
    for mine in session.query(m.Grant).filter_by(principal_type="user", principal_id=user.id):
        abilities = ROLES.get(mine.role, set()) - {"create"}
        if mine.scope_id is None or not abilities:
            continue
        obj = session.get(m.SCOPE_MODELS[mine.scope_type], mine.scope_id)
        if obj is None or (mine.scope_type, obj.id) in seen:
            continue
        others = [g for g, _ in grants_on(session, mine.scope_type, obj)
                  if not (g.principal_type == "user" and g.principal_id == user.id)
                  and ROLES.get(g.role, set()) & abilities
                  and _principal_active(session, g.principal_type, g.principal_id)]
        if not others:
            seen.add((mine.scope_type, obj.id))
            out.append((mine, obj))
    out.sort(key=lambda x: (x[0].scope_type, x[1].label))
    return out


def delete_user(session, user):
    """Remove an account, its permissions, logins and team memberships.

    The history keeps its entries: they name people, they do not point at accounts.
    Returns the number of permissions removed.
    """
    n = (session.query(m.Grant).filter_by(principal_type="user", principal_id=user.id)
         .delete(synchronize_session=False))
    session.delete(user)          # identities and team memberships go with it (cascade)
    session.flush()
    return n


def link_candidates(session):
    """[(signed-in account, imported placeholder)] that share an e-mail address.

    These are only proposals.  An identity provider asserts the address, and a
    careless or hostile one could assert someone else's, so an administrator
    confirms each link instead of it happening by itself.
    """
    placeholders = {}
    for u in session.query(m.User).filter(m.User.subject.like("pwa:%"), m.User.active.is_(True)):
        if u.email:
            placeholders.setdefault(u.email.strip().lower(), []).append(u)
    out = []
    for u in session.query(m.User).filter(~m.User.subject.like("pwa:%"), m.User.email.isnot(None)):
        for p in placeholders.get(u.email.strip().lower(), []):
            out.append((u, p))
    return out
