"""WorkspaceRegistry (V3-P74 §10) contract, on every backend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

import pytest
from testkit import FOREIGN, SCOPE, data_for, new_store, restart

from himari.common import BindingId, ConversationId, EnvironmentKind, TrustedScope, WorkspaceId
from himari.data.api import (
    MAX_ATTACHED_WORKSPACES,
    DataError,
    DataErrorCode,
    InitialActivation,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

MACHINE = EnvironmentKind("machine")
OWN = WorkspaceId("mws_own")
ME = SCOPE.principal_id
T = TypeVar("T")


def run(awaitable: Awaitable[T]) -> T:
    return asyncio.run(awaitable)  # type: ignore[arg-type]


def machine_conversation(data: InMemoryData | SqliteData) -> TrustedScope:
    """A Conversation whose machine activation's own folder is its T0 Binding."""
    return TrustedScope(ME, run(data.create_conversation(ME, activations=[InitialActivation(MACHINE, OWN)])))


def code(awaitable: Awaitable[object]) -> DataErrorCode:
    with pytest.raises(DataError) as caught:
        run(awaitable)
    return caught.value.code


def test_register_list_update_and_unregister() -> None:
    data = data_for(new_store())
    first = run(data.register_workspace(ME, MACHINE, "/home/u/projects/a", "  project   a ", "note\r\nline"))
    assert (first.location, first.label, first.note, first.attached) == ("/home/u/projects/a", "project a",
                                                                         "note\nline", 0)
    second = run(data.register_workspace(ME, MACHINE, "/", "root"))
    assert [w.workspace_id for w in run(data.list_workspaces(ME, MACHINE))] == [first.workspace_id,
                                                                                second.workspace_id]
    assert run(data.list_workspaces(FOREIGN.principal_id, MACHINE)) == ()
    assert run(data.list_workspaces(ME, EnvironmentKind("inside"))) == ()
    assert code(data.register_workspace(ME, MACHINE, "/home/u/projects/a", "again")) is DataErrorCode.DUPLICATE_IDENTITY
    edited = run(data.update_workspace(ME, first.workspace_id, label="A", note="   "))
    assert (edited.label, edited.note) == ("A", None)
    assert code(data.update_workspace(FOREIGN.principal_id, first.workspace_id, label="x", note=None)) \
        is DataErrorCode.NOT_FOUND
    assert code(data.unregister_workspace(FOREIGN.principal_id, first.workspace_id)) is DataErrorCode.NOT_FOUND
    run(data.unregister_workspace(ME, first.workspace_id))
    assert [w.workspace_id for w in run(data.list_workspaces(ME, MACHINE))] == [second.workspace_id]


@pytest.mark.parametrize("location", ["relative/path", "/a/../b", "/a/./b", "/a//b", "/a/", "/a\x00b", ""])
def test_a_location_is_an_absolute_normal_path(location: str) -> None:
    data = data_for(new_store())
    assert code(data.register_workspace(ME, MACHINE, location, "x")) is DataErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("label", ["", "   ", "x" * 101, "a\x07b"])
def test_a_label_is_one_short_line(label: str) -> None:
    data = data_for(new_store())
    assert code(data.register_workspace(ME, MACHINE, "/a", label)) is DataErrorCode.INVALID_ARGUMENT


def test_a_long_note_is_refused() -> None:
    data = data_for(new_store())
    assert code(data.register_workspace(ME, MACHINE, "/a", "a", "x" * 1001)) is DataErrorCode.TOO_LARGE


def test_the_filesystem_is_kept_apart_from_the_note() -> None:
    """§14: what the Environment found is not the user's to edit; it survives a restart."""
    store = new_store()
    data = data_for(store)
    drive = run(data.register_workspace(ME, MACHINE, "/drive/a", "a", "x" * 1000, "fuse.rclone"))
    assert (drive.note, drive.filesystem) == ("x" * 1000, "fuse.rclone")
    run(data.update_workspace(ME, drive.workspace_id, label="a", note=None))
    data = data_for(restart(store))
    assert [(w.note, w.filesystem) for w in run(data.list_workspaces(ME, MACHINE))] == [(None, "fuse.rclone")]
    assert run(data.register_workspace(ME, MACHINE, "/b", "b")).filesystem is None
    assert code(data.register_workspace(ME, MACHINE, "/c", "c", None, "ext 4")) is DataErrorCode.INVALID_ARGUMENT
    assert code(data.register_workspace(ME, MACHINE, "/c", "c", None, "")) is DataErrorCode.INVALID_ARGUMENT


def test_attach_makes_it_the_working_one_and_detach_falls_back() -> None:
    store = new_store()
    data = data_for(store)
    scope = machine_conversation(data)
    before = run(data.read_current_state_basis(scope)).state_revision
    own = run(data.read_conversation_workspaces(scope, MACHINE))
    assert own.working == own.attached[0].binding.binding_id and own.attached[0].workspace is None
    a = run(data.register_workspace(ME, MACHINE, "/p/a", "a"))
    b = run(data.register_workspace(ME, MACHINE, "/p/b", "b"))
    bound_a = run(data.attach_workspace(scope, a.workspace_id))
    bound_b = run(data.attach_workspace(scope, b.workspace_id))
    assert bound_b.cwd_relative == "" and bound_b.environment_instance_id == bound_a.environment_instance_id
    found = run(data.read_conversation_workspaces(scope, MACHINE))
    assert found.working == bound_b.binding_id
    assert [x.workspace.workspace_id if x.workspace else None for x in found.attached] == [None, a.workspace_id,
                                                                                            b.workspace_id]
    assert found.attached[1].workspace is not None and found.attached[1].workspace.attached == 1
    assert run(data.resolve_default_binding(scope, MACHINE)) == bound_b  # what the Tools are given
    assert run(data.read_current_state_basis(scope)).state_revision > before  # the model is told
    assert code(data.attach_workspace(scope, a.workspace_id)) is DataErrorCode.DUPLICATE_IDENTITY
    assert code(data.unregister_workspace(ME, a.workspace_id)) is DataErrorCode.INVALID_STATE_TRANSITION
    run(data.set_working_workspace(scope, bound_a.binding_id))
    assert run(data.read_conversation_workspaces(scope, MACHINE)).working == bound_a.binding_id
    run(data.detach_workspace(scope, bound_a.binding_id))  # the working one: the last attached left takes over
    assert run(data.read_conversation_workspaces(scope, MACHINE)).working == bound_b.binding_id
    run(data.detach_workspace(scope, bound_b.binding_id))  # none left: back to the own folder
    found = run(data.read_conversation_workspaces(scope, MACHINE))
    assert found.working == own.working and len(found.attached) == 1
    assert code(data.detach_workspace(scope, own.working or BindingId("x"))) is DataErrorCode.INVALID_STATE_TRANSITION
    assert code(data.detach_workspace(scope, BindingId("bind_none"))) is DataErrorCode.NOT_FOUND
    assert code(data.set_working_workspace(scope, BindingId("bind_none"))) is DataErrorCode.NOT_FOUND
    run(data.unregister_workspace(ME, a.workspace_id))  # detached everywhere: it may go
    store = restart(store)
    assert [w.workspace_id for w in run(data_for(store).list_workspaces(ME, MACHINE))] == [b.workspace_id]


def test_attaching_needs_the_kind_active_and_the_principals_own_workspace() -> None:
    data = data_for(new_store())
    plain = TrustedScope(ME, run(data.create_conversation(ME)))
    mine = run(data.register_workspace(ME, MACHINE, "/p/a", "a"))
    assert code(data.attach_workspace(plain, mine.workspace_id)) is DataErrorCode.INVALID_STATE_TRANSITION
    assert code(data.read_conversation_workspaces(plain, MACHINE)) is DataErrorCode.NOT_FOUND
    theirs = run(data.register_workspace(FOREIGN.principal_id, MACHINE, "/p/a", "a"))  # each principal has their own
    scope = machine_conversation(data)
    assert code(data.attach_workspace(scope, theirs.workspace_id)) is DataErrorCode.NOT_FOUND
    other = TrustedScope(FOREIGN.principal_id, scope.conversation_id)
    assert code(data.attach_workspace(other, theirs.workspace_id)) is DataErrorCode.NOT_AUTHORIZED
    assert code(data.read_conversation_workspaces(other, MACHINE)) is DataErrorCode.NOT_AUTHORIZED


def test_at_most_so_many_attached() -> None:
    data = data_for(new_store())
    scope = machine_conversation(data)
    for n in range(MAX_ATTACHED_WORKSPACES):
        run(data.attach_workspace(scope, run(data.register_workspace(ME, MACHINE, f"/p/{n}", str(n))).workspace_id))
    extra = run(data.register_workspace(ME, MACHINE, "/p/extra", "extra"))
    assert code(data.attach_workspace(scope, extra.workspace_id)) is DataErrorCode.LIMIT_EXCEEDED


def test_a_purged_conversation_lets_its_workspaces_go() -> None:
    data = data_for(new_store())
    scope = machine_conversation(data)
    a = run(data.register_workspace(ME, MACHINE, "/p/a", "a"))
    run(data.attach_workspace(scope, a.workspace_id))
    run(data.trash_conversation(scope))
    assert code(data.unregister_workspace(ME, a.workspace_id)) is DataErrorCode.INVALID_STATE_TRANSITION  # the trash too
    run(data.begin_purge(scope))
    run(data.purge_conversation(scope))
    assert run(data.list_workspaces(ME, MACHINE))[0].attached == 0
    run(data.unregister_workspace(ME, a.workspace_id))


def test_unknown_conversation_is_not_authorized() -> None:
    data = data_for(new_store())
    missing = TrustedScope(ME, ConversationId("conv_missing"))
    assert code(data.read_conversation_workspaces(missing, MACHINE)) is DataErrorCode.NOT_AUTHORIZED
