import pytest
import base64
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blogging"))

spec = importlib.util.spec_from_file_location("root_api_main", ROOT / "api" / "main.py")
api_main = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["root_api_main"] = api_main
spec.loader.exec_module(api_main)
api_main.DeepthoughtImageProcessRequest.model_rebuild()


def test_decode_image_rejects_invalid_base64() -> None:
    try:
        api_main._decode_image("***not-valid***")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "not valid base64" in str(exc)


def test_process_image_success_path(monkeypatch) -> None:
    def fake_build_atomic_matrices(_: bytes) -> dict:
        return {
            "width": 1,
            "height": 1,
            "original": [[[1, 2, 3]]],
            "edge_detection": [[[4, 5, 6]]],
            "pca_color_reduction": [[[7, 8, 9]]],
            "object_crops": [
                {
                    "transformation": "object_crop_detection",
                    "rgb_matrix": [[[11, 12, 13]]],
                    "bbox": {"x": 0, "y": 0, "width": 1, "height": 1},
                }
            ],
        }

    def fake_persist_atomic_nodes(**kwargs) -> str:
        assert kwargs["width"] == 1
        assert kwargs["height"] == 1
        assert len(kwargs["object_crops"]) == 1
        assert kwargs["object_crops"][0]["transformation"] == "object_crop_detection"
        return "img-123"

    monkeypatch.setattr(api_main, "_build_atomic_matrices", fake_build_atomic_matrices)
    monkeypatch.setattr(api_main, "_persist_atomic_nodes", fake_persist_atomic_nodes)

    request = api_main.DeepthoughtImageProcessRequest(
        image_base64=base64.b64encode(b"dummy-bytes").decode("utf-8")
    )
    response = api_main.process_image(request)

    assert response.success is True
    assert response.image_node_id == "img-123"
    assert response.errors == []


def test_detect_object_crops_finds_component() -> None:
    np = pytest.importorskip("numpy")

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    rgb[2:5, 2:5] = [255, 255, 255]

    edge = np.zeros((8, 8), dtype=np.uint8)
    edge[2:5, 2:5] = 200

    crops = api_main._detect_object_crops(rgb, edge)

    assert len(crops) >= 1
    first = crops[0]
    assert first["transformation"] == "object_crop_detection"
    assert first["bbox"]["width"] >= 3
    assert first["bbox"]["height"] >= 3


def test_build_atomic_matrices_orchestrates_steps(monkeypatch) -> None:
    class FakeArray:
        shape = (2, 3, 3)

        def tolist(self):
            return [[[1, 2, 3]]]

    fake_rgb = FakeArray()
    fake_edge_rgb = FakeArray()
    fake_pca = FakeArray()

    monkeypatch.setattr(api_main, "_require_image_processing_dependencies", lambda: ("np", "Image"))
    monkeypatch.setattr(api_main, "_load_rgb_matrix", lambda image_bytes, np, Image: fake_rgb)
    monkeypatch.setattr(api_main, "_compute_edge_detection_rgb", lambda rgb_matrix, np: ("edge", fake_edge_rgb))
    monkeypatch.setattr(api_main, "_compute_pca_reduction_rgb", lambda rgb_matrix, np: fake_pca)
    monkeypatch.setattr(
        api_main,
        "_detect_object_crops",
        lambda rgb_matrix, edge: [{"transformation": "object_crop_detection", "rgb_matrix": [[[9, 9, 9]]], "bbox": {"x": 0, "y": 0, "width": 1, "height": 1}}],
    )

    result = api_main._build_atomic_matrices(b"img-bytes")

    assert result["width"] == 3
    assert result["height"] == 2
    assert result["object_crops"][0]["transformation"] == "object_crop_detection"


def test_persist_atomic_nodes_orchestrates_helpers(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_require_neo4j_driver", lambda: "GraphDatabase")
    monkeypatch.setattr(api_main, "_get_neo4j_connection_settings", lambda: ("bolt://localhost", "neo4j", "password"))
    monkeypatch.setattr(api_main, "_build_atomic_feature_nodes", lambda edge, pca, crops: [{"id": "atomic-1"}])

    captured = {}

    def fake_write_atomic_nodes_to_neo4j(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(api_main, "_write_atomic_nodes_to_neo4j", fake_write_atomic_nodes_to_neo4j)

    result = api_main._persist_atomic_nodes(
        original_b64="base64",
        image_id="img-1",
        width=10,
        height=5,
        original_matrix=[[[1, 2, 3]]],
        edge_matrix=[[[4, 5, 6]]],
        pca_matrix=[[[7, 8, 9]]],
        object_crops=[],
    )

    assert result == "img-1"
    assert captured["GraphDatabase"] == "GraphDatabase"
    assert captured["uri"] == "bolt://localhost"
    assert captured["atomic_nodes"] == [{"id": "atomic-1"}]
