"""La NAS: comprobación de montaje y órdenes por SSH."""

import subprocess
from pathlib import Path

from .config import load_config, tmp_dir
from .errors import NasNotMountedError


def assert_nas_mounted(cfg: dict | None = None):
    """
    Comprueba que la NAS está realmente montada antes de crear/escribir nada,
    mirando un archivo centinela que solo existe en la NAS real (nunca en un
    disco local que haya quedado "debajo" por un montaje caído). Lanza
    NasNotMountedError (un RuntimeError) si no lo encuentra.
    """
    sentinel = (cfg or load_config()).get("nas_sentinel_file")
    if not sentinel:
        return  # sin centinela configurado, no se comprueba nada
    if not Path(sentinel).exists():
        raise NasNotMountedError(
            f"No encuentro el archivo centinela ({sentinel}) — parece que la NAS "
            f"no está montada. Abortando para no crear/escribir nada en el disco "
            f"local por error."
        )


class NasShell:
    """Órdenes en la propia NAS por SSH (config: nas_ssh), para lo que SMB no permite."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.scfg = self.cfg.get("nas_ssh") or {}

    @property
    def configured(self) -> bool:
        return bool(self.scfg.get("host") and self.scfg.get("user"))

    def command(self, remote_cmd: str) -> list[str]:
        known_hosts = tmp_dir(self.cfg) / "ssh_known_hosts"
        return [
            "ssh",
            "-i", self.scfg.get("key_file", "/config/ssh/id_ed25519"),
            "-p", str(self.scfg.get("port", 22)),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            f"{self.scfg['user']}@{self.scfg['host']}",
            remote_cmd,
        ]

    def run(self, remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(self.command(remote_cmd), capture_output=True, text=True, timeout=timeout)
