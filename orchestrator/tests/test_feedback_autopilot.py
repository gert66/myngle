import json, tempfile
from pathlib import Path
from unittest import mock
from core import feedback_autopilot as f


def row(fid="abc12345", comment="I cannot save the call outcome"):
    return {"id":fid,"comment":comment,"company_name":"Acme","country":"DE","created_at":"2026-09-16T10:00:00Z","updated_at":"2026-09-16T10:00:00Z","created_by_email":"carla@example.com"}

def test_classification():
    assert f.classify_feedback("Thank you, the alterations are really helping a lot!")=="positive"
    assert f.classify_feedback("Mazzardo - invalid number")=="data_quality"
    assert f.classify_feedback("Is it possible to automatically assign the company owner?")=="feature_request"
    assert f.classify_feedback("I cannot save the call outcome")=="bug"

def test_ingest_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        old=f.CASES_DIR; f.CASES_DIR=Path(d)
        try:
            a,new1=f.ingest_row(row()); b,new2=f.ingest_row(row())
            assert new1 is True and new2 is False and a["feedback_id"]==b["feedback_id"]
        finally: f.CASES_DIR=old

def test_positive_is_informational():
    with tempfile.TemporaryDirectory() as d:
        old=f.CASES_DIR; f.CASES_DIR=Path(d)
        try:
            c,_=f.ingest_row(row(comment="Thanks, this is really helping a lot!"))
            assert c["status"]=="informational" and c["kind"]=="positive"
        finally: f.CASES_DIR=old

def test_build_green_proposal_is_commit_bound():
    case={**row(),"feedback_id":"abc12345","reporter_email":"carla@example.com","company":"Acme"}
    state={"phase":"DONE","runtime":{"history":[{"commit":"a"*40,"paths":["src/a.ts"],"verdict":"PASS","detail":"Mismatch fixed and regression tested."}]}}
    p=f.build_proposal(case,state,expected_main_sha="b"*40)
    assert p.risk=="GREEN" and p.deployment["commit_sha"]=="a"*40 and p.deployment["expected_main_sha"]=="b"*40


def test_feature_request_queues_read_research():
    with tempfile.TemporaryDirectory() as d:
        old=f.CASES_DIR; f.CASES_DIR=Path(d)
        try:
            c,_=f.ingest_row(row(comment="Is it possible to block countries I do not call?"))
            assert c["status"]=="queued_research" and c["kind"]=="feature_request"
        finally: f.CASES_DIR=old
