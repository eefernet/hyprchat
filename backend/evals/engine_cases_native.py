"""Native-language cohort: C, C++, Rust and C# asked for the way a user would, judged by private probes.

Each case names the build system and the path of the program, because that is what the judge runs. The
probes are never shown to the builder.
"""
from engine_cases_everyday import pipe, program

CASES = [
    program('native-c',
            'Write me a small C program called wcount. Use CMake, so that cmake -S . -B build and then '
            'cmake --build build gives me build/wcount. It reads text from stdin and prints one line like '
            'lines=2 words=3. Words are separated by spaces, tabs or newlines. A last line without a newline '
            'still counts as a line. Empty input prints lines=0 words=0. Add tests I can run with ctest and a README.',
            ['./build/wcount'],
            [pipe('one two\nthree\n', 'lines=2 words=3\n'), pipe('alpha\tbeta  gamma', 'lines=1 words=3\n'),
             pipe('', 'lines=0 words=0\n'), pipe('\n\n', 'lines=2 words=0\n')],
            'Add a --chars option that puts chars=N, the number of bytes read, at the end of the same line. '
            'Without the option the output stays exactly as it is now. Any other option should print an error '
            'and exit with code 2. Update the tests and the README.',
            [pipe('ab c\n', 'lines=1 words=2 chars=5\n', '--chars'), pipe('ab c\n', 'lines=1 words=2\n'),
             {'args': ['--bogus'], 'stdin': 'x', 'code': 2, 'stderr_nonempty': True}]),
    program('native-cpp',
            'I need a C++17 command-line tool called stats, built with CMake into build/stats. I pipe in whole '
            'numbers separated by spaces or newlines and it prints min, max and sum on one line, for example '
            'min=-2 max=7 sum=9. Negative numbers are fine. If there are no numbers print the word empty. '
            'If something is not a whole number, print an error to stderr and exit with code 2. '
            'Please include tests that run with ctest, and a README.',
            ['./build/stats'],
            [pipe('4 -2 7', 'min=-2 max=7 sum=9\n'), pipe('5\n', 'min=5 max=5 sum=5\n'), pipe('', 'empty\n'),
             {'stdin': '3 x 4', 'code': 2, 'stderr_nonempty': True}],
            'Add a --mean option. With it, print a second line with the average to exactly two decimals, '
            'like mean=2.33. Keep the first line and the default output unchanged. Add tests for it.',
            [pipe('1 2 4', 'min=1 max=4 sum=7\nmean=2.33\n', '--mean'), pipe('1 2 4', 'min=1 max=4 sum=7\n'),
             pipe('-3 3', 'min=-3 max=3 sum=0\nmean=0.00\n', '--mean')]),
    program('native-rust',
            'Make a Rust command-line program with Cargo, no external crates, that turns a number into a '
            'Roman numeral. cargo run --quiet -- 1994 should print MCMXCIV. It works for 1 through 3999. '
            'Anything else, including text that is not a number or a missing argument, prints an error to '
            'stderr and exits with code 2. Include cargo tests and a README.',
            ['cargo', 'run', '--quiet', '--'],
            [{'args': ['1994'], 'stdout': 'MCMXCIV\n'}, {'args': ['4'], 'stdout': 'IV\n'},
             {'args': ['3999'], 'stdout': 'MMMCMXCIX\n'}, {'args': ['0'], 'code': 2, 'stderr_nonempty': True},
             {'args': ['abc'], 'code': 2, 'stderr_nonempty': True}],
            'Add the other direction: cargo run --quiet -- --decode MCMXCIV prints 1994. Text that is not a '
            'valid Roman numeral exits with code 2. Keep the existing behaviour and add tests.',
            [{'args': ['--decode', 'MCMXCIV'], 'stdout': '1994\n'}, {'args': ['--decode', 'XLII'], 'stdout': '42\n'},
             {'args': ['--decode', 'HELLO'], 'code': 2}, {'args': ['2024'], 'stdout': 'MMXXIV\n'}]),
    program('native-csharp',
            'Create a .NET 8 C# console app in a solution called Initials.sln. Put the app in src/Initials and '
            'xunit tests in tests/Initials.Tests. dotnet run --project src/Initials -- "ada lovelace" should '
            'print A.L. and dotnet run --project src/Initials -- "Grace Brewster Hopper" should print G.B.H. '
            'Extra spaces between names are ignored. With no name, or a name that is only spaces, print an '
            'error to stderr and exit with code 2. Include a README.',
            ['dotnet', 'run', '--no-build', '--project', 'src/Initials', '--'],
            [{'args': ['ada lovelace'], 'stdout': 'A.L.\n'}, {'args': ['Grace Brewster Hopper'], 'stdout': 'G.B.H.\n'},
             {'args': ['  linus   torvalds '], 'stdout': 'L.T.\n'}, {'args': [], 'code': 2, 'stderr_nonempty': True},
             {'args': ['   '], 'code': 2, 'stderr_nonempty': True}],
            'Add a --lower option that prints the initials in lowercase, so --lower "Ada Lovelace" prints a.l. '
            'Keep the default output as it is and add tests for the new option.',
            [{'args': ['--lower', 'Ada Lovelace'], 'stdout': 'a.l.\n'}, {'args': ['Ada Lovelace'], 'stdout': 'A.L.\n'}]),
]
