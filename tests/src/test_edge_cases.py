"""Additional coverage for the path, client and translation edge cases."""

from __future__ import annotations

import http.server
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from static_sets.api import ApiRequestError, KubernetesClient
from static_sets.errors import (
    PathSecurityError,
    SecretDisclosureError,
    TranslationError,
)
from static_sets.paths import (
    assert_owned_by_current_user,
    assert_within,
    atomic_write,
    remove_tree_within,
)
from static_sets.translation import (
    ResolvedConfig,
    compile_workload,
    referenced_config,
)

CONFIGMAP = {"web-conf": {"app.conf": b"greeting=hello\n"}}
SECRET = {"web-secret": {"token": b"s3cr3t-value"}}


# -- paths -----------------------------------------------------------------------


def test_assert_within_allows_the_root_itself_only_when_asked(tmp_path: Path) -> None:
    """The root may only be the target when the caller says so."""
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(PathSecurityError):
        assert_within(root, root)

    assert_within(root, root, allow_equal=True)


def test_atomic_write_removes_its_temporary_file_when_the_write_fails(
    tmp_path: Path,
) -> None:
    """A failed write leaves no half-written temporary file behind."""
    target = tmp_path / "file.txt"

    with pytest.raises(TypeError):
        atomic_write(target, "not bytes")  # type: ignore[arg-type]

    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_refuses_a_symlinked_parent_directory(tmp_path: Path) -> None:
    """A symlinked directory on the way to the target is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks are not available in this environment")

    with pytest.raises(PathSecurityError):
        atomic_write(link / "file.txt", b"x")


def test_remove_tree_within_refuses_a_symlinked_child(tmp_path: Path) -> None:
    """A symlink inside the tree is refused rather than followed."""
    root = tmp_path / "root"
    target = root / "a"
    target.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("safe", encoding="utf-8")
    try:
        (target / "link").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks are not available in this environment")

    with pytest.raises(PathSecurityError):
        remove_tree_within(root, target)

    assert outside.read_text(encoding="utf-8") == "safe"


def test_remove_tree_within_refuses_a_symlinked_target(tmp_path: Path) -> None:
    """A symlinked target is refused."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symlinks are not available in this environment")

    with pytest.raises(PathSecurityError):
        remove_tree_within(root, link)


def test_remove_tree_within_deletes_a_plain_file(tmp_path: Path) -> None:
    """A file target is unlinked rather than treated as a tree."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "file"
    target.write_text("x", encoding="utf-8")

    remove_tree_within(root, target)

    assert not target.exists()


def test_ownership_check_is_a_no_op_for_a_missing_path(tmp_path: Path) -> None:
    """A path that does not exist yet cannot be owned by somebody else."""
    assert_owned_by_current_user(tmp_path / "absent")

    if os.name == "posix":
        assert_owned_by_current_user(tmp_path)


# -- api -------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """A one-route handler that echoes the request it received."""

    received: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server's own naming
        """Answer 200 with a JSON body, echoing the Authorization header."""
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        }
        body = b'{"items": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:  # noqa: N802 - http.server's own naming
        """Answer 404 so the error path is exercised."""
        body = b'{"reason": "NotFound"}'
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server's signature
        """Stay quiet; the test asserts on the response, not on a log."""
        del format, args


@pytest.fixture()
def plain_server() -> Iterator[str]:
    """Serve the handler on a loopback port for the duration of a test.

    Yields:
        The server's base URL.
    """
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_default_transport_speaks_http_and_sends_its_token(plain_server: str) -> None:
    """Without an injected transport the client really does perform the request."""
    client = KubernetesClient(server=plain_server, token="t0ken", timeout=5)

    payload = client.json("GET", "/api/v1/nodes?limit=1")

    assert payload == {"items": []}
    assert _Handler.received["path"] == "/api/v1/nodes?limit=1"
    assert _Handler.received["authorization"] == "Bearer t0ken"


def test_the_default_transport_surfaces_an_http_error(plain_server: str) -> None:
    """An HTTP error status is returned rather than raised by the transport."""
    client = KubernetesClient(server=plain_server, timeout=5)

    with pytest.raises(ApiRequestError):
        client.json("DELETE", "/api/v1/namespaces/edge/pods/x")


def test_a_missing_token_file_is_not_an_error(tmp_path: Path) -> None:
    """A client without a service-account token can still be built."""
    built = KubernetesClient.from_environment(
        {
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "STATIC_SETS_SERVICE_ACCOUNT_DIR": str(tmp_path / "absent"),
        }
    )

    assert built.token is None
    assert built.ca_path is None


# -- translation -----------------------------------------------------------------


def test_referenced_config_lists_only_what_was_resolved() -> None:
    """The helper reports the objects a resolved set actually contains."""
    references = referenced_config(ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET))

    assert [(item.kind, item.name) for item in references] == [
        ("ConfigMap", "web-conf"),
        ("Secret", "web-secret"),
    ]


def test_two_sources_may_not_disagree_about_a_file(
    make_static_set: Any, make_resource: Any
) -> None:
    """Two volumes that would write different content to one path are refused."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "conf",
            "configMap": {
                "name": "web-conf",
                "items": [
                    {"key": "first", "path": "same"},
                    {"key": "second", "path": "same"},
                ],
            },
        }
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(
            resource.spec,
            ResolvedConfig(config_maps={"web-conf": {"first": b"one", "second": b"two"}}),
        )

    assert "disagree" in str(error.value)


def test_an_env_entry_must_set_a_value_or_a_source(
    make_static_set: Any, make_resource: Any
) -> None:
    """An environment entry with neither is refused rather than dropped."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "EMPTY"}]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "neither" in str(error.value)


def test_non_utf8_config_map_key_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """A binary value cannot become an environment variable."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "bin"}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(
            resource.spec,
            ResolvedConfig(config_maps={**CONFIGMAP, "bin": {"BLOB": b"\xff\xfe"}}),
        )

    assert "UTF-8" in str(error.value)


def test_non_utf8_config_map_value_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """A binary value read by key is refused as well."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "BLOB", "valueFrom": {"configMapKeyRef": {"name": "bin", "key": "BLOB"}}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(
            resource.spec,
            ResolvedConfig(config_maps={**CONFIGMAP, "bin": {"BLOB": b"\xff\xfe"}}),
        )

    assert "UTF-8" in str(error.value)


def test_an_optional_config_map_key_resolves_to_no_variable(
    make_static_set: Any, make_resource: Any
) -> None:
    """An optional key that is absent omits the variable instead of failing."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {
            "name": "MAYBE",
            "valueFrom": {
                "configMapKeyRef": {"name": "web-conf", "key": "absent", "optional": True}
            },
        }
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "env" not in workload.pod["spec"]["containers"][0]


def test_an_optional_secret_key_resolves_to_no_variable(
    make_static_set: Any, make_resource: Any
) -> None:
    """The same holds for an optional Secret key, once disclosure is allowed."""
    body = make_resource()
    body["spec"]["policy"] = {"allowInlineSecretEnv": True}
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {
            "name": "MAYBE",
            "valueFrom": {
                "secretKeyRef": {"name": "web-secret", "key": "absent", "optional": True}
            },
        }
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET)
    )

    assert "env" not in workload.pod["spec"]["containers"][0]


def test_a_non_utf8_secret_value_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """A binary Secret value cannot become an environment variable either."""
    body = make_resource()
    body["spec"]["policy"] = {"allowInlineSecretEnv": True}
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "BLOB", "valueFrom": {"secretKeyRef": {"name": "bin", "key": "BLOB"}}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError):
        compile_workload(resource.spec, ResolvedConfig(secrets={"bin": {"BLOB": b"\xff"}}))


def test_a_missing_opted_in_secret_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """Opting into disclosure does not weaken the resolution rules."""
    body = make_resource()
    body["spec"]["policy"] = {"allowInlineSecretEnv": True}
    body["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "absent", "key": "k"}}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "absent" in str(error.value)


def test_an_unread_config_map_volume_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """A volume naming an object the installer could not read is refused."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "unread"}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError) as error:
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert "unread" in str(error.value)


def test_pod_level_non_root_requires_an_explicit_secret_mode(
    make_static_set: Any, make_resource: Any
) -> None:
    """A pod-level security context is honoured, not only the container's."""
    body = make_resource()
    body["spec"]["template"]["spec"]["securityContext"] = {"runAsNonRoot": True}
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret"}},
    ]
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "conf", "mountPath": "/etc/web"},
        {"name": "creds", "mountPath": "/etc/creds"},
    ]
    resource = make_static_set(**body)

    with pytest.raises(Exception, match="defaultMode"):
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET))


def test_a_pod_level_root_user_satisfies_the_secret_mode_check(
    make_static_set: Any, make_resource: Any
) -> None:
    """A pod-level ``runAsUser: 0`` is enough to accept the 0400 default."""
    body = make_resource()
    body["spec"]["template"]["spec"]["securityContext"] = {"runAsUser": 0}
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf"}},
        {"name": "creds", "secret": {"secretName": "web-secret"}},
    ]
    body["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "conf", "mountPath": "/etc/web"},
        {"name": "creds", "mountPath": "/etc/creds"},
    ]
    resource = make_static_set(**body)

    workload = compile_workload(
        resource.spec, ResolvedConfig(config_maps=CONFIGMAP, secrets=SECRET)
    )

    assert workload.modes["secrets/creds/token"] == 0o400


def test_an_optional_config_map_volume_that_is_absent_is_empty(
    make_static_set: Any, make_resource: Any
) -> None:
    """An optional volume resolves to a directory with no files."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {"name": "conf", "configMap": {"name": "web-conf", "optional": True}}
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig())

    assert workload.files == {}
    assert workload.pod["spec"]["volumes"][0]["hostPath"]["path"] == "config/conf"


def test_items_of_an_optional_volume_may_be_absent(
    make_static_set: Any, make_resource: Any
) -> None:
    """Optional items that are missing are simply not materialized."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "conf",
            "configMap": {
                "name": "web-conf",
                "optional": True,
                "items": [{"key": "absent", "path": "absent"}],
            },
        }
    ]
    resource = make_static_set(**body)

    workload = compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))

    assert workload.files == {}


def test_a_mode_out_of_range_is_refused(make_static_set: Any, make_resource: Any) -> None:
    """A file mode outside the permission bits is refused."""
    body = make_resource()
    body["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "conf",
            "configMap": {
                "name": "web-conf",
                "items": [{"key": "app.conf", "path": "a", "mode": 4096}],
            },
        }
    ]
    resource = make_static_set(**body)

    with pytest.raises(TranslationError):
        compile_workload(resource.spec, ResolvedConfig(config_maps=CONFIGMAP))


def test_a_harness_secret_error_is_named_for_the_operator(
    make_static_set: Any, make_resource: Any
) -> None:
    """Resolving a Secret by envFrom reports the disclosure precisely."""
    body = make_resource()
    body["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "web-secret"}}
    ]
    resource = make_static_set(**body)

    with pytest.raises(SecretDisclosureError) as error:
        compile_workload(resource.spec, ResolvedConfig(secrets=SECRET))

    assert "secretRef" in str(error.value)
