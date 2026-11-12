import pytest
from coder_progress import display_command

MASK = '•••'
JWT = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefgh'

CASES = [
    ('npm test', 'npm test'),
    ('PORT=8000 npm start', 'npm start'),
    ('export FOO=bar npm run build', 'npm run build'),
    ('DATABASE_PASSWORD=hunter2 python app.py', 'python app.py'),
    ('MY_SECRET=x ./run', './run'),
    ('https://user:pw@host/p', f'https://{MASK}@host/p'),
    ('git clone https://user:pw@host/p.git', f'git clone https://{MASK}@host/p.git'),
    ('cli --token abc', f'cli --token {MASK}'),
    ('cli --api-key=abc --verbose', f'cli --api-key {MASK}'),
    ('docker run -e API_KEY=abc123 image', f'docker run -e API_KEY={MASK} image'),
    ('cat <<EOF > f.txt\nsecret body\nEOF', 'cat <<EOF > f.txt'),
    ('npm   test\r\nrm -rf /', 'npm test'),
    ('\x1b[31mnpm\x1b[0m test', 'npm test'),
    ('pytest -h', 'pytest -h'),
    ('curl -H "Authorization: Bearer x" https://a', f'curl -H {MASK}'),
    ('python3 /root/projects/proj-abc123/src/components/HeaderComponent.py --verbose', 'python3 /root/projects/proj-abc123/src/components/HeaderComponent.py --verbose'),
    ('node /root/.daedalus/jobs/cw3-13fb755c/workspace/server/startServerWithOptions.js', 'node /root/.daedalus/jobs/cw3-13fb755c/workspace/server/startServerWithOptions.js'),
    ('', ''),
    (None, ''),
]


@pytest.mark.parametrize('value,expected', CASES)
def test_display_command_table(value, expected):
    assert display_command(value) == expected


@pytest.mark.parametrize('command,secret', [
    ('curl -H "Authorization: Bearer xyz" https://x', 'xyz'),
    ('git push https://ghp_' + 'a' * 30 + '@github.com/o/r', 'ghp_' + 'a' * 30),
    ('aws s3 ls --profile p AKIAIOSFODNN7EXAMPLE', 'AKIAIOSFODNN7EXAMPLE'),
    ('echo ' + JWT, 'eyJ'),
    ('echo deadbeefdeadbeefdeadbeefdeadbeef', 'deadbeef'),
    ('echo ' + 'Zm9v' * 12 + '==', 'Zm9v' * 3),
    ('API_TOKEN=abc123 curl -H "Authorization: Bearer xyz" https://u:p@h/x --password hunter2', 'abc123'),
])
def test_secrets_never_survive(command, secret):
    shown = display_command(command)
    assert secret not in shown and MASK in shown
    assert shown.split(' ', 1)[0] in {'curl', 'git', 'aws', 'echo'}


def test_bearer_tail_and_userinfo_both_drop():
    shown = display_command('API_TOKEN=abc123 curl -H "Authorization: Bearer xyz" https://u:p@h/x --password hunter2')
    assert shown.startswith('curl') and not any(s in shown for s in ('abc123', 'xyz', 'hunter2', 'u:p'))


def test_long_commands_are_capped_with_ellipsis():
    shown = display_command(' '.join(f'--opt{i} value{i}' for i in range(30)))
    assert len(shown) <= 120 and shown.endswith('…') and shown.startswith('--opt0 value0')
    assert len(display_command('x' * 10 + ' y' * 100, limit=40)) == 40


def test_limit_leaves_short_commands_alone():
    assert display_command('npm test', limit=8) == 'npm test'
