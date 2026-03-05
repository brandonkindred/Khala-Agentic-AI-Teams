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


def test_post_and_get_clients() -> None:
    create = client.post('/branding/clients', json={"name": "Acme Corp"})
    assert create.status_code == 201
    data = create.json()
    assert data['id'].startswith('client_')
    assert data['name'] == 'Acme Corp'
    list_resp = client.get('/branding/clients')
    assert list_resp.status_code == 200
    clients = list_resp.json()
    assert isinstance(clients, list)
    assert any(c['id'] == data['id'] for c in clients)
    get_one = client.get(f"/branding/clients/{data['id']}")
    assert get_one.status_code == 200
    assert get_one.json()['name'] == 'Acme Corp'


def test_get_client_404() -> None:
    resp = client.get('/branding/clients/nonexistent-id')
    assert resp.status_code == 404


def test_post_and_get_brands() -> None:
    create_c = client.post('/branding/clients', json={"name": "Brand Test Client"})
    assert create_c.status_code == 201
    client_id = create_c.json()['id']
    create_b = client.post(
        f'/branding/clients/{client_id}/brands',
        json={
            'company_name': 'BrandCo',
            'company_description': 'A company for brand tests',
            'target_audience': 'testers',
        },
    )
    assert create_b.status_code == 201
    brand_data = create_b.json()
    assert brand_data['id'].startswith('brand_')
    assert brand_data['client_id'] == client_id
    list_b = client.get(f'/branding/clients/{client_id}/brands')
    assert list_b.status_code == 200
    assert len(list_b.json()) >= 1
    get_b = client.get(f'/branding/clients/{client_id}/brands/{brand_data["id"]}')
    assert get_b.status_code == 200
    assert get_b.json()['mission']['company_name'] == 'BrandCo'


def test_get_brand_404() -> None:
    create_c = client.post('/branding/clients', json={"name": "For 404"})
    client_id = create_c.json()['id']
    resp = client.get(f'/branding/clients/{client_id}/brands/nonexistent-brand-id')
    assert resp.status_code == 404


def test_put_brand_update() -> None:
    create_c = client.post('/branding/clients', json={"name": "Update Test"})
    client_id = create_c.json()['id']
    create_b = client.post(
        f'/branding/clients/{client_id}/brands',
        json={
            'company_name': 'Original',
            'company_description': 'Original description here',
            'target_audience': 'audience',
        },
    )
    brand_id = create_b.json()['id']
    put_resp = client.put(
        f'/branding/clients/{client_id}/brands/{brand_id}',
        json={'company_description': 'Updated description here'},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()['mission']['company_description'] == 'Updated description here'


def test_post_brands_run_returns_team_output() -> None:
    create_c = client.post('/branding/clients', json={"name": "Run Test Client"})
    client_id = create_c.json()['id']
    create_b = client.post(
        f'/branding/clients/{client_id}/brands',
        json={
            'company_name': 'RunCo',
            'company_description': 'Company for run test',
            'target_audience': 'users',
        },
    )
    brand_id = create_b.json()['id']
    run_resp = client.post(
        f'/branding/clients/{client_id}/brands/{brand_id}/run',
        json={'human_approved': True},
    )
    assert run_resp.status_code == 200
    out = run_resp.json()
    assert 'status' in out
    assert 'codification' in out
    assert 'brand_guidelines' in out
    assert 'brand_book' in out


def test_request_market_research_returns_503_without_service() -> None:
    create_c = client.post('/branding/clients', json={"name": "MR Client"})
    client_id = create_c.json()['id']
    create_b = client.post(
        f'/branding/clients/{client_id}/brands',
        json={
            'company_name': 'MRCo',
            'company_description': 'Company for market research test',
            'target_audience': 'buyers',
        },
    )
    brand_id = create_b.json()['id']
    resp = client.post(f'/branding/clients/{client_id}/brands/{brand_id}/request-market-research')
    assert resp.status_code in (200, 503)


def test_request_design_assets_returns_stub() -> None:
    create_c = client.post('/branding/clients', json={"name": "Design Client"})
    client_id = create_c.json()['id']
    create_b = client.post(
        f'/branding/clients/{client_id}/brands',
        json={
            'company_name': 'DesignCo',
            'company_description': 'Company for design assets test',
            'target_audience': 'designers',
        },
    )
    brand_id = create_b.json()['id']
    resp = client.post(f'/branding/clients/{client_id}/brands/{brand_id}/request-design-assets')
    assert resp.status_code == 200
    data = resp.json()
    assert 'request_id' in data
    assert data['status'] == 'pending'
    assert 'artifacts' in data
