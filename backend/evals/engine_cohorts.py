"""One definition of the qualification cohorts: prompt bank, repetitions and pass floor.

The composer driver, the judge identity receipt and the grader each kept their own copy of these tables.
A cohort added in one place and not the others produced a report the grader could not read.
"""
from engine_cases import CASES as CLASSIC, CASES_V1, DEVELOPMENT, DEVELOPMENT_V1, PROMPT_BANK_VERSION
from engine_cases_everyday import CASES as EVERYDAY, POCKET, REHEARSAL, breadth_cases
from engine_cases_reliability import CASES as RELIABILITY

# Cohorts the visible-composer driver can run.
COMPOSER = {'classic': CLASSIC, 'development': DEVELOPMENT, 'everyday': EVERYDAY, 'pocket': POCKET,
            'breadth': breadth_cases()}
# Banks hashed into the judge identity receipt.
IDENTITY = {'development': DEVELOPMENT, 'classic': CLASSIC, 'everyday': EVERYDAY, 'pocket': POCKET,
            'breadth': COMPOSER['breadth'], 'rehearsal': REHEARSAL}
_REPEATS = {'development': 2, 'pocket': 1, 'breadth': 1}
_FULL = {'development', 'pocket', 'breadth'}          # every workflow must pass
_V1 = {'classic': CASES_V1, 'development': DEVELOPMENT_V1}


def repeats(cohort):
    return _REPEATS.get(cohort, 3)


def floor(cohort):
    return 1.0 if cohort in _FULL else .8


def roster(cohort, bank_version=PROMPT_BANK_VERSION):
    """(cases, repetitions) as the grader expects them; version 1 wording stays readable for old reports."""
    if cohort == 'reliability':
        return RELIABILITY, 3
    cases = _V1[cohort] if bank_version != PROMPT_BANK_VERSION and cohort in _V1 else COMPOSER[cohort]
    return cases, repeats(cohort)
