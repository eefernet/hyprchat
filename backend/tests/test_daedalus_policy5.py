"""Recorded verifier failure mechanisms and persistent policy-5 boundaries."""
import copy
import hashlib
import json
import time

import jsonschema
import pytest

from coder_repository import Repository
from context_policy import DEFAULTS


def environment(tmp_path, **extra):
    project=tmp_path/'project';project.mkdir(exist_ok=True)
    repo=Repository(project,tmp_path/'repo');repo.refresh()
    store=WorkerStore(tmp_path/'worker')
    payload={'policy_version':5,'task':'Add README.','original_task':'Add README.',
        'requirements':[{'id':'r1','kind':'documentation','text':'Add README.','source_refs':['request-1']}],
        'settings':dict(DEFAULTS),'seconds_remaining':60,'calls_remaining':20, **extra}
    store.create('op','job','verify',payload);store.update('op',started=time.time())
    return repo,store,payload


def test_recovery_identity_keeps_distinct_audits_and_revisions():
    from coder_response_recovery import contract_identity
    operation={'kind':'verify','payload':{'task':'Task','revision_id':'a',
        'evidence':{'audit_key':{'check_id':'r1','round':1},'failed_checks':[{'log':'old','reused':False}]}}}
    continued=copy.deepcopy(operation)
    continued['payload']['evidence']['failed_checks']=[{'log':'new','reused':True}]
    assert contract_identity(operation)==contract_identity(continued)
    continued['payload']['evidence']['audit_key']['round']=2
    assert contract_identity(operation)!=contract_identity(continued)
    continued=copy.deepcopy(operation);continued['payload']['revision_id']='b'
    assert contract_identity(operation)!=contract_identity(continued)


from coder_worker_runtime import WorkerStore, _read_only
