"""Document Generator Agent - creates process docs, templates, and checklists."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..shared.llm import JSONExtractionFailure, LLMClient
from ..shared.user_profile_store import UserProfileStore
from .models import (
    GenerateChecklistRequest,
    GeneratedChecklist,
    GeneratedDocument,
    GenerateDocRequest,
    GeneratedTemplate,
    GenerateTemplateRequest,
    SOPRequest,
)
from .prompts import (
    CHECKLIST_PROMPT,
    MEETING_AGENDA_PROMPT,
    PROCESS_DOC_PROMPT,
    SOP_PROMPT,
    TEMPLATE_PROMPT,
)

logger = logging.getLogger(__name__)


class DocGeneratorAgent:
    """
    Agent for generating documentation.

    Capabilities:
    - Process documentation (how-to guides)
    - Checklists for tasks
    - Templates for common documents
    - Standard Operating Procedures
    - Meeting agendas
    """

    def __init__(
        self,
        llm: LLMClient,
        profile_store: Optional[UserProfileStore] = None,
        storage_dir: Optional[str] = None,
    ) -> None:
        """
        Initialize the Document Generator Agent.

        Args:
            llm: LLM client for generation
            profile_store: User profile storage
            storage_dir: Directory for document storage
        """
        self.llm = llm
        self.profile_store = profile_store or UserProfileStore()
        self.storage_dir = Path(storage_dir or os.getenv("PA_DOCS_DIR", ".agent_cache/documents"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_user_dir(self, user_id: str) -> Path:
        """Get document directory for a user."""
        user_dir = self.storage_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _save_document(self, user_id: str, doc: GeneratedDocument) -> None:
        """Save a generated document."""
        user_dir = self._get_user_dir(user_id)
        file_ext = "md" if doc.format == "markdown" else "txt"
        file_path = user_dir / f"{doc.doc_id}.{file_ext}"
        file_path.write_text(doc.content)

        meta_path = user_dir / f"{doc.doc_id}.json"
        meta_path.write_text(
            json.dumps(
                {
                    "doc_id": doc.doc_id,
                    "doc_type": doc.doc_type,
                    "title": doc.title,
                    "format": doc.format,
                    "created_at": doc.created_at,
                },
                indent=2,
            )
        )

    def generate_process_doc(self, request: GenerateDocRequest) -> GeneratedDocument:
        """
        Generate a process documentation.

        Args:
            request: Generation request

        Returns:
            Generated document
        """
        profile = self.profile_store.load_profile(request.user_id)

        user_info = ""
        if profile:
            if profile.professional.job_title:
                user_info += f"Job: {profile.professional.job_title}\n"
            if profile.professional.industry:
                user_info += f"Industry: {profile.professional.industry}\n"

        prompt = PROCESS_DOC_PROMPT.format(
            topic=request.topic,
            context=request.context or "No additional context",
            user_info=user_info or "No specific user information",
            format=request.format,
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                expected_keys=["title", "content"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to generate process doc (JSON extraction failed):\n%s", e)
            return GeneratedDocument(
                doc_id=str(uuid4())[:8],
                doc_type="process",
                title=f"Guide: {request.topic}",
                content=f"# {request.topic}\n\nFailed to generate documentation due to JSON extraction error.\nSee logs for recovery suggestions.",
                format=request.format,
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error("Failed to generate process doc: %s", e)
            return GeneratedDocument(
                doc_id=str(uuid4())[:8],
                doc_type="process",
                title=f"Guide: {request.topic}",
                content=f"# {request.topic}\n\nFailed to generate documentation.",
                format=request.format,
                created_at=datetime.utcnow().isoformat(),
            )

        doc = GeneratedDocument(
            doc_id=str(uuid4())[:8],
            doc_type="process",
            title=data.get("title", f"Guide: {request.topic}"),
            content=data.get("content", ""),
            format=request.format,
            created_at=datetime.utcnow().isoformat(),
        )

        self._save_document(request.user_id, doc)
        return doc

    def generate_checklist(self, request: GenerateChecklistRequest) -> GeneratedChecklist:
        """
        Generate a checklist for a task.

        Args:
            request: Checklist request

        Returns:
            Generated checklist
        """
        time_instruction = ""
        if request.include_time_estimates:
            time_instruction = "Include time estimates for each item."

        prompt = CHECKLIST_PROMPT.format(
            task=request.task,
            context=request.context or "No additional context",
            time_estimate_instruction=time_instruction,
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                expected_keys=["title", "items"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to generate checklist (JSON extraction failed):\n%s", e)
            return GeneratedChecklist(
                checklist_id=str(uuid4())[:8],
                title=f"Checklist: {request.task}",
                items=[
                    {
                        "item": "Failed to generate items - JSON extraction error",
                        "priority": "required",
                    }
                ],
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error("Failed to generate checklist: %s", e)
            return GeneratedChecklist(
                checklist_id=str(uuid4())[:8],
                title=f"Checklist: {request.task}",
                items=[{"item": "Failed to generate items", "priority": "required"}],
                created_at=datetime.utcnow().isoformat(),
            )

        return GeneratedChecklist(
            checklist_id=str(uuid4())[:8],
            title=data.get("title", f"Checklist: {request.task}"),
            items=data.get("items", []),
            total_time_estimate=data.get("total_time_estimate"),
            created_at=datetime.utcnow().isoformat(),
        )

    def generate_template(self, request: GenerateTemplateRequest) -> GeneratedTemplate:
        """
        Generate a document template.

        Args:
            request: Template request

        Returns:
            Generated template
        """
        prompt = TEMPLATE_PROMPT.format(
            template_type=request.template_type,
            purpose=request.purpose,
            fields=", ".join(request.fields) if request.fields else "Determine appropriate fields",
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                expected_keys=["title", "content", "fields"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to generate template (JSON extraction failed):\n%s", e)
            return GeneratedTemplate(
                template_id=str(uuid4())[:8],
                template_type=request.template_type,
                title=f"Template: {request.template_type}",
                content="Failed to generate template - JSON extraction error. See logs for recovery suggestions.",
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error("Failed to generate template: %s", e)
            return GeneratedTemplate(
                template_id=str(uuid4())[:8],
                template_type=request.template_type,
                title=f"Template: {request.template_type}",
                content="Failed to generate template.",
                created_at=datetime.utcnow().isoformat(),
            )

        return GeneratedTemplate(
            template_id=str(uuid4())[:8],
            template_type=request.template_type,
            title=data.get("title", f"Template: {request.template_type}"),
            content=data.get("content", ""),
            placeholders=data.get("placeholders", []),
            created_at=datetime.utcnow().isoformat(),
        )

    def generate_sop(self, request: SOPRequest) -> GeneratedDocument:
        """
        Generate a Standard Operating Procedure.

        Args:
            request: SOP request

        Returns:
            Generated SOP document
        """
        steps_text = ""
        if request.steps:
            steps_text = "\n".join(f"- {step}" for step in request.steps)
        else:
            steps_text = "Determine appropriate steps"

        safety_section = ""
        if request.include_safety:
            safety_section = "Include a Safety Considerations section."

        troubleshooting_section = ""
        if request.include_troubleshooting:
            troubleshooting_section = "Include a Troubleshooting section."

        additional_sections = ""
        if request.include_safety:
            additional_sections += "6. Safety considerations\n"
        if request.include_troubleshooting:
            additional_sections += "7. Troubleshooting guide\n"

        prompt = SOP_PROMPT.format(
            process_name=request.process_name,
            description=request.description,
            steps=steps_text,
            safety_section=safety_section,
            troubleshooting_section=troubleshooting_section,
            additional_sections=additional_sections,
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                expected_keys=["title", "content"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to generate SOP (JSON extraction failed):\n%s", e)
            return GeneratedDocument(
                doc_id=str(uuid4())[:8],
                doc_type="sop",
                title=f"SOP: {request.process_name}",
                content=f"# {request.process_name}\n\nFailed to generate SOP - JSON extraction error. See logs.",
                format="markdown",
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error("Failed to generate SOP: %s", e)
            return GeneratedDocument(
                doc_id=str(uuid4())[:8],
                doc_type="sop",
                title=f"SOP: {request.process_name}",
                content=f"# SOP: {request.process_name}\n\nFailed to generate SOP.",
                format="markdown",
                created_at=datetime.utcnow().isoformat(),
            )

        doc = GeneratedDocument(
            doc_id=str(uuid4())[:8],
            doc_type="sop",
            title=data.get("title", f"SOP: {request.process_name}"),
            content=data.get("content", ""),
            format="markdown",
            created_at=datetime.utcnow().isoformat(),
        )

        self._save_document(request.user_id, doc)
        return doc

    def generate_meeting_agenda(
        self,
        user_id: str,
        purpose: str,
        duration: int = 60,
        attendees: Optional[List[str]] = None,
        topics: Optional[List[str]] = None,
    ) -> GeneratedDocument:
        """
        Generate a meeting agenda.

        Args:
            user_id: The user ID
            purpose: Meeting purpose
            duration: Meeting duration in minutes
            attendees: List of attendees
            topics: Topics to cover

        Returns:
            Generated agenda document
        """
        prompt = MEETING_AGENDA_PROMPT.format(
            purpose=purpose,
            duration=f"{duration} minutes",
            attendees=", ".join(attendees) if attendees else "TBD",
            topics="\n".join(f"- {t}" for t in topics) if topics else "Determine based on purpose",
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                expected_keys=["title", "content"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to generate meeting agenda (JSON extraction failed):\n%s", e)
            return GeneratedDocument(
                doc_id=str(uuid4())[:8],
                doc_type="agenda",
                title=f"Meeting: {purpose}",
                content=f"# Meeting Agenda\n\nPurpose: {purpose}\n\nFailed to generate agenda - JSON extraction error. See logs.",
                format="markdown",
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error("Failed to generate meeting agenda: %s", e)
            return GeneratedDocument(
                doc_id=str(uuid4())[:8],
                doc_type="agenda",
                title=f"Meeting: {purpose}",
                content=f"# Meeting Agenda\n\nPurpose: {purpose}\n\nFailed to generate agenda.",
                format="markdown",
                created_at=datetime.utcnow().isoformat(),
            )

        doc = GeneratedDocument(
            doc_id=str(uuid4())[:8],
            doc_type="agenda",
            title=data.get("title", f"Meeting: {purpose}"),
            content=data.get("content", ""),
            format="markdown",
            created_at=datetime.utcnow().isoformat(),
        )

        self._save_document(user_id, doc)
        return doc

    def list_documents(self, user_id: str, doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List user's generated documents.

        Args:
            user_id: The user ID
            doc_type: Filter by document type

        Returns:
            List of document metadata
        """
        user_dir = self._get_user_dir(user_id)
        documents = []

        for meta_file in user_dir.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text())
                if doc_type is None or meta.get("doc_type") == doc_type:
                    documents.append(meta)
            except Exception as e:
                logger.warning("Failed to load document metadata: %s", e)

        documents.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return documents

    def get_document(self, user_id: str, doc_id: str) -> Optional[str]:
        """
        Get a document's content.

        Args:
            user_id: The user ID
            doc_id: Document ID

        Returns:
            Document content or None
        """
        user_dir = self._get_user_dir(user_id)

        for ext in ["md", "txt"]:
            file_path = user_dir / f"{doc_id}.{ext}"
            if file_path.exists():
                return file_path.read_text()

        return None

    def delete_document(self, user_id: str, doc_id: str) -> bool:
        """
        Delete a document.

        Args:
            user_id: The user ID
            doc_id: Document ID

        Returns:
            True if deleted
        """
        user_dir = self._get_user_dir(user_id)
        deleted = False

        for ext in ["md", "txt", "json"]:
            file_path = user_dir / f"{doc_id}.{ext}"
            if file_path.exists():
                file_path.unlink()
                deleted = True

        return deleted
