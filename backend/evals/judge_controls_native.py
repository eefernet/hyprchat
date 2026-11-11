"""Good and broken fixtures for the native cohort. Each pair differs in one source file by one defect."""
_C = r'''#include <ctype.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    int chars = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--chars")) chars = 1;
        else { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
    }
    long lines = 0, words = 0, bytes = 0; int c, last = '\n', inside = 0;
    while ((c = getchar()) != EOF) {
        bytes++;
        if (c == '\n') lines++;
        if (isspace(c)) inside = 0; else if (!inside) { inside = 1; words++; }
        last = c;
    }
    if (LASTLINE) lines++;
    printf("lines=%ld words=%ld", lines, words);
    if (chars) printf(" chars=%ld", bytes);
    printf("\n");
    return 0;
}
'''
_C_CMAKE = 'cmake_minimum_required(VERSION 3.16)\nproject(wcount LANGUAGES C)\nadd_executable(wcount main.c)\n'
_CPP = r'''#include <cstdio>
#include <iostream>
#include <string>
#include <vector>
int main(int argc, char **argv) {
    bool mean = false;
    for (int i = 1; i < argc; i++) {
        if (std::string(argv[i]) == "--mean") mean = true;
        else { std::cerr << "unknown option\n"; return 2; }
    }
    std::vector<long long> values; std::string token;
    while (std::cin >> token) {
        try { size_t used; long long x = std::stoll(token, &used); if (used != token.size()) throw 1; values.push_back(x); }
        catch (...) { std::cerr << "not a whole number: " << token << "\n"; return 2; }
    }
    if (values.empty()) { std::cout << "empty\n"; return 0; }
    long long low = values[0], high = values[0], sum = 0;
    for (long long x : values) { if (x < low) low = x; if (x > high) high = x; sum += x; }
    std::cout << "min=" << low << " max=" << high << " sum=" << sum << std::endl;
    if (mean) std::printf("mean=%.2f\n", MEAN);
    return 0;
}
'''
_CPP_CMAKE = 'cmake_minimum_required(VERSION 3.16)\nproject(stats LANGUAGES CXX)\nset(CMAKE_CXX_STANDARD 17)\nadd_executable(stats main.cpp)\n'
_RUST = r'''use std::env;
use std::process::exit;
const TABLE: [(u32, &str); 13] = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")];
fn encode(mut n: u32) -> String {
    let mut out = String::new();
    for (value, numeral) in TABLE.iter() { while n >= *value { out.push_str(numeral); n -= *value; } }
    out
}
fn decode(text: &str) -> Option<u32> {
    let mut n = 0; let mut rest = text;
    for (value, numeral) in TABLE.iter() { while rest.starts_with(numeral) { n += *value; rest = &rest[numeral.len()..]; } }
    if rest.is_empty() && n >= 1 && n <= 3999 && encode(n) == text { Some(n) } else { None }
}
fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    let out = if args.len() == 2 && args[0] == "--decode" { decode(&args[1]).map(|n| n.to_string()) }
        else if args.len() == 1 { args[0].parse::<u32>().ok().filter(|n| *n >= LOWEST && *n <= 3999).map(encode) }
        else { None };
    match out { Some(text) => println!("{}", text), None => { eprintln!("error: invalid input"); exit(2) } }
}
'''
_CARGO = '[package]\nname = "roman"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'
_GUID = '{6C1B2F5A-0C3E-4D55-9A51-3D2B7C1E8F10}'
_SLN = ('Microsoft Visual Studio Solution File, Format Version 12.00\n# Visual Studio Version 17\n'
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Initials", "src\\Initials\\Initials.csproj", "' + _GUID + '"\nEndProject\n'
        'Global\n\tGlobalSection(SolutionConfigurationPlatforms) = preSolution\n\t\tDebug|Any CPU = Debug|Any CPU\n\t\tRelease|Any CPU = Release|Any CPU\n\tEndGlobalSection\n'
        '\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
        + ''.join(f'\t\t{_GUID}.{c}|Any CPU.{k} = {c}|Any CPU\n' for c in ('Debug', 'Release') for k in ('ActiveCfg', 'Build.0'))
        + '\tEndGlobalSection\nEndGlobal\n')
_CSPROJ = ('<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n    <TargetFramework>net8.0</TargetFramework>\n'
           '    <ImplicitUsings>enable</ImplicitUsings>\n    <Nullable>enable</Nullable>\n  </PropertyGroup>\n</Project>\n')
_CS = r'''bool lower = args.Length > 0 && args[0] == "--lower";
var rest = lower ? args.Skip(1).ToArray() : args;
var parts = string.Join(" ", rest).Split(' ', StringSplitOptions.RemoveEmptyEntries);
if (parts.Length == 0) { Console.Error.WriteLine("error: a name is required"); return 2; }
var text = string.Concat(parts.Select(p => char.ToUpperInvariant(p[0]) + "."));
Console.WriteLine(lower ? LOWERED : text);
return 0;
'''


def pairs():
    """(kind, good files, broken files, case id) judged with the follow-up probes included."""
    def both(files, name, token, good, broken):
        return {**files, name: files[name].replace(token, good)}, {**files, name: files[name].replace(token, broken)}
    return [
        ('native-c', *both({'CMakeLists.txt': _C_CMAKE, 'main.c': _C}, 'main.c', 'LASTLINE', "last != '\\n'", '0'), 'native-c'),
        ('native-cpp', *both({'CMakeLists.txt': _CPP_CMAKE, 'main.cpp': _CPP}, 'main.cpp', 'MEAN',
                             '(double)sum / values.size()', '(double)(sum / (long long)values.size())'), 'native-cpp'),
        ('native-rust', *both({'Cargo.toml': _CARGO, 'src/main.rs': _RUST}, 'src/main.rs', 'LOWEST', '1', '0'), 'native-rust'),
        ('native-csharp', *both({'Initials.sln': _SLN, 'src/Initials/Initials.csproj': _CSPROJ, 'src/Initials/Program.cs': _CS},
                                'src/Initials/Program.cs', 'LOWERED', 'text.ToLowerInvariant()', 'text'), 'native-csharp'),
    ]
