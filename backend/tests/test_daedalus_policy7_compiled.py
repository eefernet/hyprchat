"""Compiled-language evidence: native runners supply observation and provenance.

Policy 7 could only observe Python (line tracer) and Node (V8 coverage), so a green
mvn/ctest/dotnet/go/cargo run recorded zero assertions and was force-failed. These
cases run tiny real projects through the actual check pipeline; they need the
toolchains, so they execute on Codebox and skip elsewhere.
"""
import shutil
import time

import pytest

from coder_project_runtime import contract, run_revision
from tests.test_daedalus_policy7 import fixture

POM = '''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>demo</groupId><artifactId>calc</artifactId><version>1.0</version>
<properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
<version>5.10.2</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId>
<version>3.2.5</version></plugin></plugins></build></project>'''

CMAKE = '''cmake_minimum_required(VERSION 3.16)
project(calc LANGUAGES {lang})
add_library(calc calc.{ext})
add_executable(test_calc tests/test_calc.{ext})
target_link_libraries(test_calc PRIVATE calc)
target_include_directories(test_calc PRIVATE ${{CMAKE_SOURCE_DIR}})
enable_testing()
add_test(NAME calc_adds COMMAND test_calc)
'''

PROJECTS = {
    'maven': ('mvn', {'pom.xml': POM,
        'src/main/java/demo/Calc.java': 'package demo;\npublic class Calc { public static int add(int a,int b){return a+b;} }\n',
        'src/test/java/demo/CalcTest.java': 'package demo;\nimport org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.assertEquals;\n'
            'class CalcTest { @Test void adds(){ assertEquals(5, Calc.add(2,3)); } }\n'},
        'src/test/java/demo/CalcTest.java', 'src/main/java/demo/Calc.java'),
    'cmake_c': ('ctest', {'CMakeLists.txt': CMAKE.format(lang='C', ext='c'), 'calc.h': 'int add(int a,int b);\n',
        'calc.c': '#include "calc.h"\nint add(int a,int b){return a+b;}\n',
        'tests/test_calc.c': '#include <assert.h>\n#include "calc.h"\nint main(void){assert(add(2,3)==5);return 0;}\n'},
        'tests/test_calc.c', 'calc.c'),
    'cmake_cpp': ('ctest', {'CMakeLists.txt': CMAKE.format(lang='CXX', ext='cpp'), 'calc.h': 'int add(int a,int b);\n',
        'calc.cpp': '#include "calc.h"\nint add(int a,int b){return a+b;}\n',
        'tests/test_calc.cpp': '#include <cassert>\n#include "calc.h"\nint main(){assert(add(2,3)==5);return 0;}\n'},
        'tests/test_calc.cpp', 'calc.cpp'),
    'go': ('go', {'go.mod': 'module demo/calc\n\ngo 1.21\n', 'calc.go': 'package calc\n\nfunc Add(a, b int) int { return a + b }\n',
        'calc_test.go': 'package calc\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif Add(2, 3) != 5 {\n\t\tt.Fatal("bad sum")\n\t}\n}\n'},
        'calc_test.go', 'calc.go'),
    'cargo': ('cargo', {'Cargo.toml': '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
        'src/lib.rs': 'pub fn add(a: i32, b: i32) -> i32 { a + b }\n',
        'tests/adds.rs': 'use calc::add;\n\n#[test]\nfn adds() { assert_eq!(add(2, 3), 5); }\n'},
        'tests/adds.rs', 'src/lib.rs'),
}


def run(tmp_path, files):
    repo, store = fixture(tmp_path, files)
    current = store.get('check')
    store.update('check', payload={**current['payload'], 'seconds_remaining': 900}, started=time.time())
    profile = contract(repo, None)
    return run_revision(store, 'check', repo, profile, project_id='compiled-' + tmp_path.name), profile


@pytest.mark.parametrize('name', sorted(PROJECTS))
def test_native_runner_tests_are_observed_with_application_provenance(tmp_path, name):
    tool, files, test_file, source_file = PROJECTS[name]
    if not shutil.which(tool) or not shutil.which('bwrap'):
        pytest.skip(tool + ' toolchain is only on Codebox')
    result, profile = run(tmp_path, files)
    failed = [(c['id'], c.get('failure_kind'), (c.get('log_tail') or '')[-600:]) for c in result['checks'] if not c.get('passed')]
    assert not failed, failed
    tests = [c for c in result['checks'] if c.get('is_test')]
    assert tests, [c['id'] for c in result['checks']]
    row = tests[0]
    assert row['coverage_observed'] and row['execution_succeeded'] and row['assertions_executed'] >= 1, row
    assert test_file in [f['path'] for f in row['test_files']], row['test_files']
    assert source_file in [b['path'] for b in row['source_bindings']], row['source_bindings']
    assert not any(c['id'] == 'immutable-source' for c in result['checks'])


@pytest.mark.parametrize('name', ['go', 'cmake_c'])
def test_a_native_project_without_executed_tests_is_still_rejected(tmp_path, name):
    tool, files, test_file, _ = PROJECTS[name]
    if not shutil.which(tool) or not shutil.which('bwrap'):
        pytest.skip(tool + ' toolchain is only on Codebox')
    files = {k: v for k, v in files.items() if k != test_file}
    if name == 'cmake_c':
        files['CMakeLists.txt'] = 'cmake_minimum_required(VERSION 3.16)\nproject(calc LANGUAGES C)\nadd_library(calc calc.c)\nenable_testing()\n'
    result, _ = run(tmp_path, files)
    tests = [c for c in result['checks'] if c.get('is_test')]
    assert not any(c.get('coverage_observed') and c.get('passed') for c in tests), tests


def test_dotnet_xunit_solution_is_observed_with_application_provenance(tmp_path):
    import os
    import subprocess
    if not shutil.which('dotnet') or not shutil.which('bwrap'):
        pytest.skip('dotnet toolchain is only on Codebox')
    scaffold = tmp_path / 'scaffold'; scaffold.mkdir()
    env = {**os.environ, 'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_NOLOGO': '1'}
    for command in ('dotnet new sln -n Calc', 'dotnet new classlib -o Calc --framework net8.0', 'dotnet new xunit -o Calc.Tests --framework net8.0',
                    'dotnet sln add Calc/Calc.csproj Calc.Tests/Calc.Tests.csproj', 'dotnet add Calc.Tests/Calc.Tests.csproj reference Calc/Calc.csproj'):
        subprocess.run(command, shell=True, cwd=scaffold, env=env, check=True, capture_output=True, timeout=300)
    (scaffold / 'Calc/Class1.cs').write_text('namespace Calc;\npublic static class Adder { public static int Add(int a, int b) => a + b; }\n')
    (scaffold / 'Calc.Tests/UnitTest1.cs').write_text('using Xunit;\nnamespace Calc.Tests;\npublic class AdderTests { [Fact] public void Adds() => Assert.Equal(5, Calc.Adder.Add(2, 3)); }\n')
    files = {p.relative_to(scaffold).as_posix(): p.read_text() for p in scaffold.rglob('*')
             if p.is_file() and not {'bin', 'obj'} & set(p.relative_to(scaffold).parts)}
    result, _ = run(tmp_path, files)
    failed = [(c['id'], c.get('failure_kind'), (c.get('log_tail') or '')[-600:]) for c in result['checks'] if not c.get('passed')]
    assert not failed, failed
    row = next(c for c in result['checks'] if c.get('is_test'))
    assert row['coverage_observed'] and row['execution_succeeded'] and row['assertions_executed'] >= 1, row
    assert 'Calc.Tests/UnitTest1.cs' in [f['path'] for f in row['test_files']]
    assert 'Calc/Class1.cs' in [b['path'] for b in row['source_bindings']]
    assert not any(c['id'] == 'immutable-source' for c in result['checks'])
