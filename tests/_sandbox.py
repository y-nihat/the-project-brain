"""Can this machine run the sandbox tests at all?

The gate used to ask whether a Docker daemon answers a ping — a PROXY for what
these tests actually need, which is the ability to BUILD and RUN a sandbox
image. The two come apart: on a host whose daemon is reachable but whose image
store cannot exec (observed: `StoreError: Unexpected OS error "Exec format
error", errno=8` on WSL2 while the same tests pass inside the brain container),
the proxy said "available", seven tests ran, and seven failed for a reason that
had nothing to do with the code under test. The pre-push gate then blocked a
healthy push, and its own comment claimed these tests "SKIP rather than fail
when no daemon is reachable" — true, and beside the point.

So the gate probes the capability itself, once per session, and reports WHY it
declined. Skipping is honest; failing for the environment is not.

Cost is a trivial image build. On a machine that can build, the base layer is
already cached and it is milliseconds; on one that cannot, it fails instantly.
Either way the tests were going to build images.
"""
from __future__ import annotations

import io

from brain import config

_PROBE_TAG = "brain-sandbox-probe:tmp"
_PROBE = b'FROM %s\nRUN python -c "print(1)"\n'


def _probe() -> tuple[bool, str]:
    try:
        import docker
    except ImportError:
        return False, "the docker SDK is not installed"
    try:
        client = (docker.DockerClient(base_url=config.DOCKER_HOST)
                  if config.DOCKER_HOST else docker.from_env())
    except Exception as e:  # noqa: BLE001
        return False, f"no Docker daemon: {type(e).__name__}"
    try:
        client.ping()
    except Exception as e:  # noqa: BLE001
        return False, f"Docker daemon not reachable: {type(e).__name__}"
    try:
        client.images.build(
            fileobj=io.BytesIO(_PROBE % config.SANDBOX_BASE_IMAGE.encode()),
            tag=_PROBE_TAG, rm=True)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, (f"Docker is reachable but cannot build a sandbox image "
                       f"here ({type(e).__name__}: {str(e)[:120]}) — run the "
                       f"suite inside the brain container")
    finally:
        try:
            client.images.remove(_PROBE_TAG, force=True)
        except Exception:  # noqa: BLE001
            pass
        client.close()


SANDBOX_READY, SANDBOX_SKIP_REASON = _probe()
