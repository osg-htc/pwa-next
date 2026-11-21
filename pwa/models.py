"""Database model.

Everything a mesh needs is stored in native pSConfig/pScheduler form (test
specs, archiver definitions, ISO 8601 durations), so publishing is assembly,
not translation.  SQLAlchemy 1.4 is used because that is what EL9 ships; the
JSON columns work on PostgreSQL (production) and SQLite (tests, development).
"""
import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer,
                        String, Table, Text, UniqueConstraint)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def now():
    return datetime.datetime.utcnow()


# --------------------------------------------------------------------------
# People and authorization
# --------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    # Stable identifier asserted by the identity provider (OIDC "sub").
    subject = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    email = Column(String(255))
    idp = Column(String(255))          # home organisation, as reported by CILogon
    is_admin = Column(Boolean, default=False, nullable=False)   # service administrator
    active = Column(Boolean, default=True, nullable=False)
    created = Column(DateTime, default=now)
    last_login = Column(DateTime)
    memberships = relationship("TeamMember", back_populates="user", cascade="all, delete-orphan")
    identities = relationship("UserIdentity", back_populates="user", cascade="all, delete-orphan")

    @property
    def label(self):
        return self.name or self.email or self.subject

    @property
    def is_placeholder(self):
        """Imported from the old PWA and not yet tied to a federated login."""
        return self.subject.startswith("pwa:")


class UserIdentity(Base):
    """An additional login that belongs to an existing account.

    CILogon issues a different subject for each identity provider a person
    uses (university, ORCID, ...), so one person can have several.
    """
    __tablename__ = "user_identities"
    subject = Column(String(255), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    idp = Column(String(255))
    added = Column(DateTime, default=now)
    user = relationship("User", back_populates="identities")


class Team(Base):
    """A named set of people (an experiment, a site, an operations group)."""
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(Text, default="")
    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")


class TeamMember(Base):
    __tablename__ = "team_members"
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    is_manager = Column(Boolean, default=False, nullable=False)   # may add and remove members
    team = relationship("Team", back_populates="members")
    user = relationship("User", back_populates="memberships")


class Grant(Base):
    """One permission: <principal> holds <role> on <scope>.

    scope_id NULL means "every object of scope_type".  See pwa/authz.py for
    what each role allows.
    """
    __tablename__ = "grants"
    id = Column(Integer, primary_key=True)
    principal_type = Column(String(8), nullable=False)    # "user" | "team"
    principal_id = Column(Integer, nullable=False)
    role = Column(String(16), nullable=False)
    scope_type = Column(String(16), nullable=False)       # host|hostgroup|test|archive|mesh
    scope_id = Column(Integer)
    created = Column(DateTime, default=now)
    created_by = Column(String(255))
    __table_args__ = (UniqueConstraint("principal_type", "principal_id", "role",
                                       "scope_type", "scope_id"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now, index=True)
    actor = Column(String(255))
    action = Column(String(40))
    object_type = Column(String(16))
    object_id = Column(Integer)
    object_label = Column(String(255))
    detail = Column(JSON)


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

host_archives = Table(
    "host_archives", Base.metadata,
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True),
    Column("archive_id", ForeignKey("archives.id", ondelete="CASCADE"), primary_key=True))

group_members = Table(
    "group_members", Base.metadata,
    Column("group_id", ForeignKey("hostgroups.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True))

group_excludes = Table(
    "group_excludes", Base.metadata,
    Column("group_id", ForeignKey("hostgroups.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True))

group_includes = Table(
    "group_includes", Base.metadata,
    Column("parent_id", ForeignKey("hostgroups.id", ondelete="CASCADE"), primary_key=True),
    Column("child_id", ForeignKey("hostgroups.id", ondelete="CASCADE"), primary_key=True))

mesh_archives = Table(
    "mesh_archives", Base.metadata,
    Column("mesh_id", ForeignKey("meshes.id", ondelete="CASCADE"), primary_key=True),
    Column("archive_id", ForeignKey("archives.id", ondelete="CASCADE"), primary_key=True))


class Host(Base):
    __tablename__ = "hosts"
    id = Column(Integer, primary_key=True)
    hostname = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255))         # set by a person; overrides `site`
    site = Column(String(255))                 # site name from the Lookup Service
    display_url = Column(String(500))          # NULL = https://<hostname>/toolkit/
    source = Column(String(16), default="manual", nullable=False)   # "lookup" | "manual"
    communities = Column(JSON, default=list)   # from the Lookup Service
    capabilities = Column(JSON, default=list)  # pScheduler test types the host offers
    addresses = Column(JSON, default=list)     # [{"address": "...", "family": 4|6}]
    info = Column(JSON, default=dict)          # remaining Lookup Service attributes
    no_agent = Column(Boolean, default=False, nullable=False)
    last_seen = Column(DateTime)               # last time the Lookup Service listed it
    legacy_id = Column(String(32), index=True)
    created = Column(DateTime, default=now)
    updated = Column(DateTime, default=now, onupdate=now)
    archives = relationship("Archive", secondary=host_archives, order_by="Archive.name")
    groups = relationship("HostGroup", secondary=group_members, back_populates="members",
                          order_by="HostGroup.name")

    object_type = "host"

    @property
    def label(self):
        return self.hostname

    @property
    def shown_name(self):
        return self.display_name or self.site or ""


class HostGroup(Base):
    """A set of hosts: named hosts, plus other groups, plus an optional rule.

    Effective membership (pwa/services.py) is
        members  +  members of every included group  +  hosts matching `rule`
        -  excludes
    """
    __tablename__ = "hostgroups"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    # Test type this group is meant for; narrows the host suggestions.
    intended_test = Column(String(40))
    rule = Column(JSON)        # {"match": "all"|"any", "conditions": [{field, op, value}]}
    legacy_id = Column(String(32), index=True)
    created = Column(DateTime, default=now)
    updated = Column(DateTime, default=now, onupdate=now)
    members = relationship("Host", secondary=group_members, back_populates="groups",
                           order_by="Host.hostname")
    excludes = relationship("Host", secondary=group_excludes, order_by="Host.hostname")
    includes = relationship(
        "HostGroup", secondary=group_includes,
        primaryjoin=id == group_includes.c.parent_id,
        secondaryjoin=id == group_includes.c.child_id,
        backref="included_by", order_by="HostGroup.name")

    object_type = "hostgroup"

    @property
    def label(self):
        return self.name


class Test(Base):
    """What to measure and how often."""
    __tablename__ = "tests"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    type = Column(String(40), nullable=False)       # pScheduler test type
    spec = Column(JSON, default=dict, nullable=False)   # pScheduler spec, without source/dest/schema
    tools = Column(JSON, default=list)              # preferred pScheduler tools, in order
    repeat = Column(String(40))                     # ISO 8601 duration; NULL = runs continuously
    slip = Column(String(40))
    sliprand = Column(Boolean, default=True)
    legacy_id = Column(String(32), index=True)
    created = Column(DateTime, default=now)
    updated = Column(DateTime, default=now, onupdate=now)

    object_type = "test"

    @property
    def label(self):
        return self.name


class Archive(Base):
    """Where results are sent.  `definition` is a complete pSConfig archive object."""
    __tablename__ = "archives"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    definition = Column(JSON, nullable=False)
    legacy_id = Column(String(32), index=True)
    created = Column(DateTime, default=now)
    updated = Column(DateTime, default=now, onupdate=now)

    object_type = "archive"

    @property
    def label(self):
        return self.name

    @property
    def archiver(self):
        return (self.definition or {}).get("archiver")


class Mesh(Base):
    """A published configuration: a list of tasks sharing a set of archives."""
    __tablename__ = "meshes"
    id = Column(Integer, primary_key=True)
    slug = Column(String(120), unique=True, nullable=False)    # /pub/config/<slug>
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    legacy_id = Column(String(32), index=True)
    created = Column(DateTime, default=now)
    updated = Column(DateTime, default=now, onupdate=now)
    archives = relationship("Archive", secondary=mesh_archives, order_by="Archive.name")
    tasks = relationship("MeshTask", back_populates="mesh", order_by="MeshTask.position",
                         cascade="all, delete-orphan")

    object_type = "mesh"

    @property
    def label(self):
        return self.name


class MeshTask(Base):
    __tablename__ = "mesh_tasks"
    id = Column(Integer, primary_key=True)
    mesh_id = Column(Integer, ForeignKey("meshes.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, default=0, nullable=False)
    name = Column(String(255), nullable=False)
    topology = Column(String(16), default="mesh", nullable=False)    # "mesh" | "disjoint"
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False)
    group_a_id = Column(Integer, ForeignKey("hostgroups.id"), nullable=False)
    group_b_id = Column(Integer, ForeignKey("hostgroups.id"))        # disjoint only
    enabled = Column(Boolean, default=True, nullable=False)
    mesh = relationship("Mesh", back_populates="tasks")
    test = relationship("Test")
    group_a = relationship("HostGroup", foreign_keys=[group_a_id])
    group_b = relationship("HostGroup", foreign_keys=[group_b_id])


class Setting(Base):
    """Small site-wide settings (branding), edited in the UI by administrators."""
    __tablename__ = "settings"
    key = Column(String(80), primary_key=True)
    value = Column(JSON)


class Publication(Base):
    """Result of the last attempt to publish each file."""
    __tablename__ = "publications"
    path = Column(String(300), primary_key=True)       # "config/<slug>" or "auto/<hostname>"
    sha256 = Column(String(64))
    published = Column(DateTime)                       # last time the live file changed
    checked = Column(DateTime)
    ok = Column(Boolean, default=True, nullable=False)
    message = Column(Text, default="")                 # validation error when not ok


SCOPE_MODELS = {"host": Host, "hostgroup": HostGroup, "test": Test,
                "archive": Archive, "mesh": Mesh}
