import functools
import re
import itertools
import os
from os import chdir, getcwd
from pathlib import Path
import shutil
import subprocess
from typing import Optional
from semantic_version.base import Always, BaseSpec
import logging
from .utils import file_to_package

from .dependencies import (
    ClassifierAvailability, Dependency, DependencyClassifier, PackageCache, SimpleSpec, SourcePackage, SourceRepository,
    Version
)

logger = logging.getLogger(__name__)


@BaseSpec.register_syntax
class AutoSpec(SimpleSpec):
    SYNTAX = 'autotools'

    class Parser(SimpleSpec.Parser):
        @classmethod
        def parse(cls, expression):
            blocks = [b.strip() for b in expression.split(',')]
            clause = Always()
            for block in blocks:
                if not cls.NAIVE_SPEC.match(block):
                    raise ValueError("Invalid simple block %r" % block)
                    clause &= cls.parse_block(block)

            return clause

    def __str__(self):
        # remove the whitespace to canonicalize the spec
        return ",".join(b.strip() for b in self.expression.split(','))


class AutotoolsClassifier(DependencyClassifier):
    """ This attempts to parse configure.ac in an autotool based repo.
    It supports the following macros:
        AC_INIT, AC_CHECK_HEADER, AC_CHECK_LIB, PKG_CHECK_MODULES

    BUGS:
        does not handle boost deps
        assumes ubuntu host
    """
    name = "autotools"
    description = "classifies the dependencies of native/autotools packages parsing configure.ac"

    def is_available(self) -> ClassifierAvailability:
        if shutil.which("autoconf") is None:
            return ClassifierAvailability(False, "`autoconf` does not appear to be installed! "
                                                 "Make sure it is installed and in the PATH.")
        return ClassifierAvailability(True)

    def can_classify(self, repo: SourceRepository) -> bool:
        return (repo.path / "configure.ac").exists()

    @staticmethod
    @functools.lru_cache(maxsize=128)
    def _ac_check_header(header_file):
        """
        Macro: AC_CHECK_HEADER
        Checks if the system header file header-file is compilable.
        https://www.gnu.org/software/autoconf/manual/autoconf-2.67/html_node/Generic-Headers.html
        """
        logger.info(f"AC_CHECK_HEADER {header_file}")
        package_name=file_to_package(f"{re.escape(header_file)}")
        return Dependency(package=package_name,
                          semantic_version=SimpleSpec("*"),
                          )

    @staticmethod
    @functools.lru_cache(maxsize=128)
    def _ac_check_lib(function):
        """
        Macro: AC_CHECK_LIB
        Checks for the presence of certain C, C++, or Fortran library archive files.
        https://www.gnu.org/software/autoconf/manual/autoconf-2.67/html_node/Libraries.html#Libraries
        """
        lib_file, function_name = function.split(".")
        logger.info(f"AC_CHECK_LIB {lib_file}")
        package_name = file_to_package(f"lib{re.escape(lib_file)}(.a|.so)")
        return Dependency(package=package_name,
                          semantic_version=SimpleSpec("*"),
                          )

    @staticmethod
    def _pkg_check_modules(module_name, version=None):
        """
        Macro: PKG_CHECK_MODULES
        The main interface between autoconf and pkg-config.
        Provides a very basic and easy way to check for the presence of a
        given package in the system.
        """
        if not version:
            version="*"
        module_file = re.escape(module_name +".pc")
        logger.info(f"PKG_CHECK_MODULES {module_file}, {version}")
        package_name = file_to_package(module_file)
        return Dependency(package=package_name,
                          semantic_version=SimpleSpec(version),
                          )

    @staticmethod
    @functools.lru_cache(maxsize=128)
    def _replace_variables(token:str, configure:str):
        """
            Search all variable occurrences in token and then try to find
            bindings for them in the configure script.
        """
        if "$" not in token:
            return token
        vars = re.findall(r"\$([a-zA-Z_0-9]+)|\${([_a-zA-Z0-9]+)}", token)
        vars = set(var for var in itertools.chain(*vars) if var)  #remove dups and empty
        for var in vars:
            logger.info(f"Trying to find bindings for {var} in configure")

            # This tries to find a single assign to the variable in question
            # ... var= "SOMETHING"
            # We ignore the fact thst variables could also appear in other constructs
            # For example:
            #    for var in THIS THAT ;
            # TODO/CHALLENGE Merge this two \/
            solutions = re.findall(f"{var}=\\s*\"([^\"]*)\"", configure)
            solutions += re.findall(f"{var}=\\s*'([^']*)'", configure)
            if len(solutions) > 1:
                logger.warning(f"Found several solutions for {var}: {solutions}")
            if len(solutions) == 0:
                logger.warning(f"No solution found for binding {var}")
                continue
            logger.info(f"Found a solution {solutions}")
            sol = (solutions+[None,])[0]
            if sol is not None:
                token = token.replace(f"${var}", sol).replace(f"${{{var}}}", sol)
        if "$" in token:
            raise Exception(f"Could not find a binding for variable/s in {token}")
        return token

    def _get_dependencies(self, path: str):
        logger.info(f"Getting dependencies for autotool repo {os.path.abspath(path)}")
        orig_dir = getcwd()
        chdir(path)
        try:
            trace = subprocess.check_output(
                ["autoconf", "-t", 'AC_CHECK_HEADER:$n:$1',
                             "-t", 'AC_CHECK_LIB:$n:$1.$2',
                             "-t", 'PKG_CHECK_MODULES:$n:$2',
                             "-t", 'PKG_CHECK_MODULES_STATIC:$n', "./configure.ac"]).decode("utf8")
            configure = subprocess.check_output(["autoconf","./configure.ac"]).decode("utf8")
        finally:
            chdir(orig_dir)

        deps = []
        for macro in trace.split('\n'):
            logger.debug(f"Handling: {macro}")
            macro, *arguments = macro.split(":")
            try:
                arguments = tuple(self._replace_variables(arg, configure) for arg in arguments)
            except Exception as e:
                logger.info(str(e))
                continue
            try:
                if macro == "AC_CHECK_HEADER":
                    deps.append(self._ac_check_header(header_file=arguments[0]))
                elif macro == "AC_CHECK_LIB":
                    deps.append(self._ac_check_lib(function=arguments[0]))
                elif macro == "PKG_CHECK_MODULES":
                    module_name, *version = arguments[0].split(" ")
                    deps.append(self._pkg_check_modules(module_name=module_name, version="".join(version)))
                else:
                    logger.error("Macro not supported", macro)
            except Exception as e:
                logger.error(str(e))
                continue

        '''
        # Identity of this package.
        PACKAGE_NAME='Bitcoin Core'
        PACKAGE_TARNAME='bitcoin'
        PACKAGE_VERSION='21.99.0'
        PACKAGE_STRING='Bitcoin Core 21.99.0'
        PACKAGE_BUGREPORT='https://github.com/bitcoin/bitcoin/issues'
        PACKAGE_URL='https://bitcoincore.org/'''
        package_name = self._replace_variables("$PACKAGE_NAME", configure)
        package_version = self._replace_variables("$PACKAGE_VERSION", configure)

        yield SourcePackage(
            name=package_name,
            version=Version.coerce(package_version),
            source=self,
            dependencies=deps,
            source_path=Path(path)
        )

    def classify(self, repo: SourceRepository, cache: Optional[PackageCache] = None):
        repo.extend(self._get_dependencies(str(repo.path)))