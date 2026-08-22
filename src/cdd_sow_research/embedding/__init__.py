"""Portable UI installation policy and browser-flow primitives."""

from .manifest import (
    Installation,
    InstallationManifest,
    LoadedInstallationManifest,
    ManifestValidationError,
    VerifierPolicy,
    load_installation_manifest,
)

__all__ = [
    "Installation",
    "InstallationManifest",
    "LoadedInstallationManifest",
    "ManifestValidationError",
    "VerifierPolicy",
    "load_installation_manifest",
]
