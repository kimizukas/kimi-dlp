from __future__ import annotations

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class OptionalBuildExt(build_ext):
    """A compiler failure must not make the pure-Python package unusable."""

    def run(self):
        try:
            super().run()
        except Exception as exc:
            print(f"WARNING: optional Cython extension was skipped: {exc}")

    def build_extension(self, ext):
        try:
            super().build_extension(ext)
        except Exception as exc:
            print(f"WARNING: could not build {ext.name}: {exc}")


extensions = []
try:
    from Cython.Build import cythonize

    extensions = cythonize(
        [
            Extension(
                "kimi.extractor._youtube_fast",
                ["kimi/extractor/_youtube_fast.pyx"],
            )
        ],
        compiler_directives={
            "language_level": 3,
            "boundscheck": False,
            "wraparound": False,
            "initializedcheck": False,
            "cdivision": True,
        },
        annotate=False,
    )
except ImportError:
    print("Cython is not installed; youtube.py will use its Python fallback.")
except Exception as exc:
    print(f"WARNING: could not prepare optional Cython extension: {exc}")

setup(
    ext_modules=extensions,
    cmdclass={"build_ext": OptionalBuildExt},
    package_data={"kimi.extractor": ["_youtube_fast.pyx"]},
)
