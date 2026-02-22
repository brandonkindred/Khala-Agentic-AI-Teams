from fastapi.testclient import TestClient

from branding_team.api.main import app


client = TestClient(app)


def _payload() -> dict:
    return {
        'company_name': 'Northstar Labs',
        'company_description': 'A strategic studio helping product teams ship cohesive digital experiences',
        'target_audience': 'enterprise product leaders',
    }


def test_create_session_and_get_questions() -> None:
    create = client.post('/branding/sessions', json=_payload())
    assert create.status_code == 200
    data = create.json()
    assert data['session_id']
    assert data['status'] == 'awaiting_user_answers'
    assert len(data['open_questions']) >= 1

    questions = client.get(f"/branding/sessions/{data['session_id']}/questions")
    assert questions.status_code == 200
    assert questions.json()


def test_answer_question_updates_session_and_output() -> None:
    create = client.post('/branding/sessions', json=_payload())
    session = create.json()
    session_id = session['session_id']
    question_id = session['open_questions'][0]['id']

    answer = client.post(
        f'/branding/sessions/{session_id}/questions/{question_id}/answer',
        json={'answer': 'clarity, trust, craft'},
    )
    assert answer.status_code == 200
    answered = answer.json()
    assert any(item['id'] == question_id for item in answered['answered_questions'])
    assert answered['latest_output']['brand_guidelines']


def test_unknown_session_404() -> None:
    resp = client.get('/branding/sessions/not-found')
    assert resp.status_code == 404
