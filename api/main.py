"""
FastAPI application exposing the research-and-review pipeline as an HTTP endpoint.
"""

from __future__ import annotations

import logging
import os
import base64
import binascii
import io
import uuid
from typing import Any, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.llm import OllamaLLMClient
from blog_research_agent.models import ResearchBriefInput
from blog_review_agent import BlogReviewAgent, BlogReviewInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Blog Research & Review API",
    description="Runs research and review agents to produce title choices and a blog outline from a brief.",
    version="0.1.0",
)


class AudienceDetails(BaseModel):
    """Audience details for targeting the content."""

    skill_level: Optional[str] = Field(
        None,
        description="e.g. 'beginner', 'intermediate', 'expert'.",
    )
    profession: Optional[str] = Field(
        None,
        description="e.g. 'CTO', 'developer', 'data scientist'.",
    )
    hobbies: Optional[List[str]] = Field(
        None,
        description="Relevant hobbies or interests.",
    )
    other: Optional[str] = Field(
        None,
        description="Any other audience context.",
    )


class ResearchAndReviewRequest(BaseModel):
    """Request body for the research-and-review endpoint."""

    brief: str = Field(..., description="Short description of the content topic.")
    title_concept: Optional[str] = Field(
        None,
        description="Optional idea or angle for the title.",
    )
    audience: Optional[Union[AudienceDetails, str]] = Field(
        None,
        description="Audience details (object or free-text string).",
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="e.g. 'educational', 'technical deep-dive', 'persuasive'.",
    )
    max_results: int = Field(
        20,
        ge=1,
        le=50,
        description="Maximum number of references to return.",
    )


class TitleChoiceResponse(BaseModel):
    """A title choice with probability of success."""

    title: str
    probability_of_success: float


class ResearchAndReviewResponse(BaseModel):
    """Response from the research-and-review endpoint."""

    title_choices: List[TitleChoiceResponse] = Field(
        ...,
        description="Top title choices with probability of success.",
    )
    outline: str = Field(
        ...,
        description="Detailed blog outline with notes for the first draft.",
    )
    compiled_document: Optional[str] = Field(
        None,
        description="Formatted research document (sources, academic papers, similar topics).",
    )
    notes: Optional[str] = Field(
        None,
        description="High-level synthesis and suggestions from the research agent.",
    )


class DeepthoughtImageProcessRequest(BaseModel):
    """Request body for processing an image into atomic feature nodes."""

    image_base64: str = Field(..., description="Base64-encoded image bytes.")
    image_id: Optional[str] = Field(None, description="Optional external image identifier.")


class DeepthoughtImageProcessResponse(BaseModel):
    """Response body for image processing."""

    success: bool
    image_node_id: Optional[str] = None
    errors: List[str] = Field(default_factory=list)


def _format_audience(audience: Optional[Union[AudienceDetails, str]]) -> str:
    """Convert audience input to a string for the agents."""
    if audience is None:
        return ""
    if isinstance(audience, str):
        return audience.strip()
    parts = []
    if audience.skill_level:
        parts.append(f"skill level: {audience.skill_level}")
    if audience.profession:
        parts.append(f"profession: {audience.profession}")
    if audience.hobbies:
        parts.append(f"interests: {', '.join(audience.hobbies)}")
    if audience.other:
        parts.append(audience.other)
    return "; ".join(parts) if parts else ""


def _decode_image(image_base64: str) -> bytes:
    """Decode a base64 image string into raw bytes."""
    try:
        return base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64 is not valid base64 data") from exc




def _require_image_processing_dependencies() -> tuple[Any, Any]:
    """Load image processing dependencies."""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "Image processing dependencies are missing. Install numpy and pillow."
        ) from exc
    return np, Image


def _load_rgb_matrix(image_bytes: bytes, np: Any, Image: Any) -> Any:
    """Load and normalize image bytes into an RGB numpy matrix."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb_img = img.convert("RGB")
        return np.array(rgb_img, dtype=np.uint8)


def _compute_edge_detection_rgb(rgb_matrix: Any, np: Any) -> tuple[Any, Any]:
    """Compute Sobel edge magnitude and its RGB representation."""
    gray = (
        0.299 * rgb_matrix[:, :, 0]
        + 0.587 * rgb_matrix[:, :, 1]
        + 0.114 * rgb_matrix[:, :, 2]
    ).astype(np.float32)
    padded = np.pad(gray, ((1, 1), (1, 1)), mode="edge")
    gx = (
        padded[:-2, 2:]
        + 2 * padded[1:-1, 2:]
        + padded[2:, 2:]
        - padded[:-2, :-2]
        - 2 * padded[1:-1, :-2]
        - padded[2:, :-2]
    )
    gy = (
        padded[2:, :-2]
        + 2 * padded[2:, 1:-1]
        + padded[2:, 2:]
        - padded[:-2, :-2]
        - 2 * padded[:-2, 1:-1]
        - padded[:-2, 2:]
    )
    edge = np.clip(np.sqrt(gx**2 + gy**2), 0, 255).astype(np.uint8)
    edge_rgb = np.stack([edge, edge, edge], axis=-1)
    return edge, edge_rgb


def _compute_pca_reduction_rgb(rgb_matrix: Any, np: Any) -> Any:
    """Reduce color channels via PCA and reconstruct RGB matrix."""
    flat = rgb_matrix.reshape(-1, 3).astype(np.float32)
    mean = flat.mean(axis=0)
    centered = flat - mean
    cov = np.cov(centered, rowvar=False)
    eigvals, eigvecs = np.linalg.eigh(cov)
    principal = eigvecs[:, np.argsort(eigvals)[::-1][:2]]
    reduced = centered @ principal
    reconstructed = (reduced @ principal.T) + mean
    return np.clip(reconstructed, 0, 255).astype(np.uint8).reshape(rgb_matrix.shape)

def _detect_object_crops(rgb_matrix: Any, edge_matrix: Any) -> list[dict[str, Any]]:
    """Detect object-like connected components and return cropped RGB matrices."""
    import numpy as np

    threshold = max(20.0, float(edge_matrix.mean()) * 1.25)
    mask = edge_matrix > threshold

    visited = np.zeros(mask.shape, dtype=bool)
    height, width = mask.shape
    min_area = max(16, (height * width) // 400)
    crops: list[dict[str, Any]] = []

    for y in range(height):
        for x in range(width):
            if not mask[y, x] or visited[y, x]:
                continue

            stack = [(y, x)]
            visited[y, x] = True
            min_y = max_y = y
            min_x = max_x = x
            area = 0

            while stack:
                cy, cx = stack.pop()
                area += 1
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)

                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))

            if area < min_area:
                continue

            # add 1px padding where possible
            y0 = max(min_y - 1, 0)
            y1 = min(max_y + 2, height)
            x0 = max(min_x - 1, 0)
            x1 = min(max_x + 2, width)

            crop_rgb = rgb_matrix[y0:y1, x0:x1]
            crops.append(
                {
                    "transformation": "object_crop_detection",
                    "rgb_matrix": crop_rgb.tolist(),
                    "bbox": {"x": int(x0), "y": int(y0), "width": int(x1 - x0), "height": int(y1 - y0)},
                }
            )

    return crops


def _build_atomic_matrices(image_bytes: bytes) -> dict:
    """Build RGB matrices for original image and required atomic transformations."""
    np, Image = _require_image_processing_dependencies()
    rgb_matrix = _load_rgb_matrix(image_bytes, np, Image)
    edge, edge_rgb = _compute_edge_detection_rgb(rgb_matrix, np)
    pca_rgb = _compute_pca_reduction_rgb(rgb_matrix, np)
    object_crops = _detect_object_crops(rgb_matrix, edge)

    return {
        "width": int(rgb_matrix.shape[1]),
        "height": int(rgb_matrix.shape[0]),
        "original": rgb_matrix.tolist(),
        "edge_detection": edge_rgb.tolist(),
        "pca_color_reduction": pca_rgb.tolist(),
        "object_crops": object_crops,
    }




def _require_neo4j_driver() -> Any:
    """Load Neo4j driver dependency."""
    try:
        from neo4j import GraphDatabase
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("Neo4j dependency is missing. Install neo4j driver.") from exc
    return GraphDatabase


def _get_neo4j_connection_settings() -> tuple[str, str, str]:
    """Return validated Neo4j connection settings from environment."""
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not uri or not user or not password:
        raise RuntimeError("NEO4J_URI, NEO4J_USER, and NEO4J_PASSWORD must be configured")
    return uri, user, password


def _build_atomic_feature_nodes(
    edge_matrix: list,
    pca_matrix: list,
    object_crops: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build atomic feature node payloads for graph persistence."""
    atomic_nodes = [
        {
            "id": str(uuid.uuid4()),
            "transformation": "edge_detection_filter",
            "rgb_matrix": edge_matrix,
            "bbox": None,
        },
        {
            "id": str(uuid.uuid4()),
            "transformation": "pca_color_space_reduction",
            "rgb_matrix": pca_matrix,
            "bbox": None,
        },
    ]

    for crop in object_crops:
        atomic_nodes.append(
            {
                "id": str(uuid.uuid4()),
                "transformation": crop["transformation"],
                "rgb_matrix": crop["rgb_matrix"],
                "bbox": crop["bbox"],
            }
        )

    return atomic_nodes


def _write_atomic_nodes_to_neo4j(
    *,
    GraphDatabase: Any,
    uri: str,
    user: str,
    password: str,
    parent_id: str,
    original_b64: str,
    width: int,
    height: int,
    original_matrix: list,
    atomic_nodes: list[dict[str, Any]],
) -> None:
    """Write image and atomic feature nodes to Neo4j."""
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (img:Image {
                    id: $parent_id,
                    content_base64: $original_b64,
                    width: $width,
                    height: $height,
                    rgb_matrix: $original_matrix
                })
                WITH img
                UNWIND $atomic_nodes AS atomic
                CREATE (feature:AtomicImageFeature {
                    id: atomic.id,
                    transformation: atomic.transformation,
                    rgb_matrix: atomic.rgb_matrix,
                    bbox: atomic.bbox
                })
                CREATE (feature)-[:PART_OF]->(img)
                """,
                parent_id=parent_id,
                original_b64=original_b64,
                width=width,
                height=height,
                original_matrix=original_matrix,
                atomic_nodes=atomic_nodes,
            )
    finally:
        driver.close()

def _persist_atomic_nodes(
    *,
    original_b64: str,
    image_id: Optional[str],
    width: int,
    height: int,
    original_matrix: list,
    edge_matrix: list,
    pca_matrix: list,
    object_crops: list[dict[str, Any]],
) -> str:
    """Persist original image and atomic feature nodes to Neo4j."""
    GraphDatabase = _require_neo4j_driver()
    uri, user, password = _get_neo4j_connection_settings()
    parent_id = image_id or str(uuid.uuid4())
    atomic_nodes = _build_atomic_feature_nodes(edge_matrix, pca_matrix, object_crops)

    _write_atomic_nodes_to_neo4j(
        GraphDatabase=GraphDatabase,
        uri=uri,
        user=user,
        password=password,
        parent_id=parent_id,
        original_b64=original_b64,
        width=width,
        height=height,
        original_matrix=original_matrix,
        atomic_nodes=atomic_nodes,
    )

    return parent_id


# Shared LLM client and agents (initialized on first request or at startup)
_llm_client: Optional[OllamaLLMClient] = None
_research_agent: Optional[ResearchAgent] = None
_review_agent: Optional[BlogReviewAgent] = None


def _get_agents() -> tuple[ResearchAgent, BlogReviewAgent]:
    """Lazily initialize and return research and review agents."""
    global _llm_client, _research_agent, _review_agent
    if _llm_client is None:
        _llm_client = OllamaLLMClient(model="deepseek-r1", timeout=1800.0)
    if _research_agent is None:
        cache = AgentCache(cache_dir=".agent_cache")
        _research_agent = ResearchAgent(llm_client=_llm_client, cache=cache)
    if _review_agent is None:
        _review_agent = BlogReviewAgent(llm_client=_llm_client)
    return _research_agent, _review_agent


@app.post(
    "/research-and-review",
    response_model=ResearchAndReviewResponse,
    summary="Run research and review pipeline",
    description="Executes the research agent (web + arXiv search) and review agent to produce title choices and a blog outline from the given brief and audience details.",
)
def research_and_review(request: ResearchAndReviewRequest) -> ResearchAndReviewResponse:
    """
    Run the research-and-review pipeline.

    Accepts a brief, optional title concept, and audience details. Returns
    title choices, a blog outline, and the compiled research document.
    """
    try:
        research_agent, review_agent = _get_agents()
    except Exception as e:
        logger.exception("Failed to initialize agents")
        raise HTTPException(status_code=500, detail=f"Agent initialization failed: {e}") from e

    # Build brief text (include title concept if provided)
    brief_text = request.brief.strip()
    if request.title_concept:
        brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"

    audience_str = _format_audience(request.audience)

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
        max_results=request.max_results,
    )

    try:
        research_result = research_agent.run(brief_input)
    except Exception as e:
        logger.exception("Research agent failed")
        raise HTTPException(status_code=500, detail=f"Research failed: {e}") from e

    try:
        review_input = BlogReviewInput(
            brief=request.brief,
            audience=audience_str or None,
            tone_or_purpose=request.tone_or_purpose,
            references=research_result.references,
        )
        review_result = review_agent.run(review_input)
    except Exception as e:
        logger.exception("Review agent failed")
        raise HTTPException(status_code=500, detail=f"Review failed: {e}") from e

    return ResearchAndReviewResponse(
        title_choices=[
            TitleChoiceResponse(
                title=tc.title,
                probability_of_success=tc.probability_of_success,
            )
            for tc in review_result.title_choices
        ],
        outline=review_result.outline,
        compiled_document=research_result.compiled_document,
        notes=research_result.notes,
    )


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post(
    "/deepthought/images/process",
    response_model=DeepthoughtImageProcessResponse,
    summary="Process image into atomic feature nodes",
    description=(
        "Decodes an input image, generates atomic RGB matrices for edge detection, "
        "PCA color-space reduction, and object-crop detection, stores the original image node "
        "and atomic nodes in Neo4j, and links atomic nodes to the original image via "
        "PART_OF relationships."
    ),
)
def process_image(request: DeepthoughtImageProcessRequest) -> DeepthoughtImageProcessResponse:
    """Process an image and persist atomic feature nodes in Neo4j."""
    errors: list[str] = []

    try:
        image_bytes = _decode_image(request.image_base64)
    except Exception as exc:
        errors.append(str(exc))
        return DeepthoughtImageProcessResponse(success=False, errors=errors)

    try:
        matrices = _build_atomic_matrices(image_bytes)
    except Exception as exc:
        errors.append(str(exc))
        return DeepthoughtImageProcessResponse(success=False, errors=errors)

    try:
        image_node_id = _persist_atomic_nodes(
            original_b64=request.image_base64,
            image_id=request.image_id,
            width=matrices["width"],
            height=matrices["height"],
            original_matrix=matrices["original"],
            edge_matrix=matrices["edge_detection"],
            pca_matrix=matrices["pca_color_reduction"],
            object_crops=matrices["object_crops"],
        )
    except Exception as exc:
        errors.append(str(exc))
        return DeepthoughtImageProcessResponse(success=False, errors=errors)

    return DeepthoughtImageProcessResponse(success=True, image_node_id=image_node_id, errors=[])
