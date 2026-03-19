def collect_client_discovery(raw_answers: dict, questionnaire: dict) -> dict:
    merged = dict(questionnaire)
    merged.update(raw_answers)
    return merged
