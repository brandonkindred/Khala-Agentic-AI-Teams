def run_axe_scan(target: str) -> dict:
    return {"target": target, "engine": "axe", "violations": []}


def run_lighthouse_accessibility(target: str) -> dict:
    return {"target": target, "engine": "lighthouse", "score": 0.9}


def crawl_targets(targets: list[str]) -> list[str]:
    return targets
