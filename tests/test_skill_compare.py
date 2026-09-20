from scripts.compare_skill_trials import compare


def _arm(rows):
    return {"status": "executed", "details": rows}


def test_compare_blocks_new_answer_fact_errors_even_when_operations_improve():
    report = {
        "arms": {
            "B": _arm([{
                "task_id": "t1", "success": False, "tool_calls_total": 3,
                "answer_evidence_coverage": 1.0,
                "hard_verification_pass": True,
                "answer_fact_pass": True,
            }]),
            "C": _arm([{
                "task_id": "t1", "success": True, "tool_calls_total": 3,
                "answer_evidence_coverage": 1.0,
                "hard_verification_pass": False,
                "answer_fact_pass": False,
                "contradicted_claims": [{"claim": "wrong"}],
            }]),
        }
    }

    result = compare(report)

    assert result["decision"] == "reject"
    assert result["criteria"]["answer_quality_not_worse"] is False


def test_compare_keeps_citation_missing_as_diagnostic_only():
    report = {
        "arms": {
            "B": _arm([{
                "task_id": "t1", "success": False, "tool_calls_total": 3,
                "answer_evidence_coverage": None,
                "hard_verification_pass": True,
                "answer_fact_pass": True,
                "citation_binding_pass": False,
            }]),
            "C": _arm([{
                "task_id": "t1", "success": True, "tool_calls_total": 3,
                "answer_evidence_coverage": None,
                "hard_verification_pass": True,
                "answer_fact_pass": True,
                "citation_binding_pass": False,
            }]),
        }
    }

    result = compare(report)

    assert result["decision"] == "accept"
    assert result["criteria"]["answer_quality_not_worse"] is True
