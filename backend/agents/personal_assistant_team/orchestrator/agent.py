"""Personal Assistant Orchestrator - routes requests to specialist agents."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..calendar_agent.agent import CalendarAgent
from ..deal_finder_agent.agent import DealFinderAgent
from ..doc_generator_agent.agent import DocGeneratorAgent
from ..email_agent.agent import EmailAgent
from ..models import AssistantRequest, AssistantResponse
from ..reservation_agent.agent import ReservationAgent
from ..shared.credential_store import CredentialStore
from ..shared.llm import LLMClient, JSONExtractionFailure
from ..shared.user_profile_store import UserProfileStore
from ..task_agent.agent import TaskAgent
from ..user_profile_agent.agent import UserProfileAgent
from .models import AgentAction, Intent, OrchestratorRequest, OrchestratorResponse
from .prompts import INTENT_CLASSIFICATION_PROMPT, RESPONSE_GENERATION_PROMPT

logger = logging.getLogger(__name__)


class PersonalAssistantOrchestrator:
    """
    Main orchestrator for the Personal Assistant team.
    
    Routes user requests to appropriate specialist agents based on intent
    classification and manages the overall conversation flow.
    """

    def __init__(
        self,
        llm: LLMClient,
        credential_store: Optional[CredentialStore] = None,
        profile_store: Optional[UserProfileStore] = None,
    ) -> None:
        """
        Initialize the orchestrator and all specialist agents.
        
        Args:
            llm: LLM client for intent classification and response generation
            credential_store: Shared credential storage
            profile_store: Shared profile storage
        """
        self.llm = llm
        self.credential_store = credential_store or CredentialStore()
        self.profile_store = profile_store or UserProfileStore()
        
        self.email_agent = EmailAgent(llm, self.credential_store, self.profile_store)
        self.calendar_agent = CalendarAgent(llm, self.credential_store, self.profile_store)
        self.task_agent = TaskAgent(llm, profile_store=self.profile_store)
        self.deal_finder = DealFinderAgent(llm, self.profile_store)
        self.reservation_agent = ReservationAgent(llm, self.profile_store)
        self.doc_generator = DocGeneratorAgent(llm, self.profile_store)
        
        self._user_profile_agents: Dict[str, UserProfileAgent] = {}

    def _get_profile_agent(self, user_id: str) -> UserProfileAgent:
        """Get or create a UserProfileAgent for a user."""
        if user_id not in self._user_profile_agents:
            self._user_profile_agents[user_id] = UserProfileAgent(
                self.llm, user_id, self.profile_store
            )
        return self._user_profile_agents[user_id]

    def classify_intent(self, message: str) -> Intent:
        """
        Classify the intent of a user message.
        
        Uses robust JSON extraction with expected_keys for reliable parsing.
        
        Args:
            message: User's message
            
        Returns:
            Classified Intent
        """
        prompt = INTENT_CLASSIFICATION_PROMPT.format(message=message)
        
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.1,
                expected_keys=["primary_intent", "secondary_intents", "entities", "confidence"],
            )
        except JSONExtractionFailure as e:
            logger.error("Intent classification failed with JSON extraction error:\n%s", e)
            return Intent(primary="general", confidence=0.5)
        except Exception as e:
            logger.error("Intent classification failed: %s", e)
            return Intent(primary="general", confidence=0.5)
        
        return Intent(
            primary=data.get("primary_intent", "general"),
            secondary=data.get("secondary_intents", []),
            confidence=float(data.get("confidence", 0.5)),
            entities=data.get("entities", {}),
        )

    def run(self, request: OrchestratorRequest) -> OrchestratorResponse:
        """
        Process a user request through the orchestrator.
        
        Args:
            request: The orchestrator request
            
        Returns:
            OrchestratorResponse with results
        """
        intent = self.classify_intent(request.message)
        logger.info("Classified intent: %s (confidence: %.2f)", intent.primary, intent.confidence)
        
        actions: List[AgentAction] = []
        results: Dict[str, Any] = {}
        
        try:
            if intent.primary == "email":
                action_result = self._handle_email(request, intent)
                actions.append(action_result)
                results["email"] = action_result.result
            
            elif intent.primary == "calendar":
                action_result = self._handle_calendar(request, intent)
                actions.append(action_result)
                results["calendar"] = action_result.result
            
            elif intent.primary == "tasks":
                action_result = self._handle_tasks(request, intent)
                actions.append(action_result)
                results["tasks"] = action_result.result
            
            elif intent.primary == "deals":
                action_result = self._handle_deals(request, intent)
                actions.append(action_result)
                results["deals"] = action_result.result
            
            elif intent.primary == "reservations":
                action_result = self._handle_reservations(request, intent)
                actions.append(action_result)
                results["reservations"] = action_result.result
            
            elif intent.primary == "documentation":
                action_result = self._handle_documentation(request, intent)
                actions.append(action_result)
                results["documentation"] = action_result.result
            
            elif intent.primary == "profile":
                action_result = self._handle_profile(request, intent)
                actions.append(action_result)
                results["profile"] = action_result.result
            
            else:
                action_result = self._handle_general(request, intent)
                actions.append(action_result)
                results["general"] = action_result.result
        
        except Exception as e:
            logger.error("Error handling request: %s", e)
            actions.append(AgentAction(
                agent="orchestrator",
                action="error",
                result={"error": str(e)},
                success=False,
            ))
        
        profile_updates = self._check_for_profile_updates(request)
        
        response = self._generate_response(request, intent, actions, results)
        response.profile_updates = profile_updates
        
        return response

    def _handle_email(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle email-related requests."""
        message_lower = request.message.lower()
        
        if not self.email_agent.has_credentials(request.user_id):
            return AgentAction(
                agent="email",
                action="check_credentials",
                result={
                    "needs_setup": True,
                    "message": "Email is not connected. Please set up your email credentials first.",
                },
            )
        
        if any(word in message_lower for word in ["read", "check", "inbox", "new emails", "unread"]):
            from ..email_agent.models import EmailReadRequest
            emails = self.email_agent.read_emails(EmailReadRequest(
                user_id=request.user_id,
                limit=10,
                unread_only="unread" in message_lower,
            ))
            
            summaries = []
            for email in emails[:5]:
                summary = self.email_agent.summarize_email(email)
                summaries.append(summary.model_dump())
            
            return AgentAction(
                agent="email",
                action="read_inbox",
                result={"emails": summaries, "total": len(emails)},
            )
        
        elif any(word in message_lower for word in ["draft", "write", "compose", "send"]):
            from ..email_agent.models import EmailDraftRequest
            draft = self.email_agent.draft_email(EmailDraftRequest(
                user_id=request.user_id,
                intent=request.message,
                context=request.context,
            ))
            
            return AgentAction(
                agent="email",
                action="draft_email",
                result={"draft": draft.model_dump()},
            )
        
        else:
            return AgentAction(
                agent="email",
                action="general",
                result={"message": "I can help you read emails or draft new ones."},
            )

    def _handle_calendar(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle calendar-related requests."""
        message_lower = request.message.lower()
        
        if any(word in message_lower for word in ["schedule", "add", "create", "book"]):
            result = self.calendar_agent.create_event_from_text(
                user_id=request.user_id,
                text=request.message,
                auto_create=False,
            )
            
            return AgentAction(
                agent="calendar",
                action="create_event",
                result=result,
            )
        
        elif any(word in message_lower for word in ["what", "show", "list", "upcoming", "today"]):
            if "today" in message_lower:
                events = self.calendar_agent.get_today_events(request.user_id)
            else:
                events = self.calendar_agent.get_upcoming_events(request.user_id)
            
            return AgentAction(
                agent="calendar",
                action="list_events",
                result={"events": [e.model_dump() for e in events]},
            )
        
        elif any(word in message_lower for word in ["free", "available", "open"]):
            from datetime import datetime, timedelta
            from ..calendar_agent.models import ScheduleRequest
            
            suggestions = self.calendar_agent.suggest_schedule(ScheduleRequest(
                user_id=request.user_id,
                title="Meeting",
                duration_minutes=60,
                preferred_date=datetime.utcnow() + timedelta(days=1),
            ))
            
            return AgentAction(
                agent="calendar",
                action="find_availability",
                result={"suggestions": [s.model_dump() for s in suggestions]},
            )
        
        else:
            return AgentAction(
                agent="calendar",
                action="general",
                result={"message": "I can help you schedule events or check your calendar."},
            )

    def _handle_tasks(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle task/todo-related requests."""
        message_lower = request.message.lower()
        
        if any(word in message_lower for word in ["add", "buy", "get", "need", "remember"]):
            from ..task_agent.models import AddItemsFromTextRequest
            result = self.task_agent.add_items_from_text(AddItemsFromTextRequest(
                user_id=request.user_id,
                text=request.message,
            ))
            
            return AgentAction(
                agent="tasks",
                action="add_items",
                result=result,
            )
        
        elif any(word in message_lower for word in ["list", "show", "what", "pending"]):
            items = self.task_agent.get_pending_items(request.user_id)
            lists = self.task_agent.get_all_lists(request.user_id)
            
            return AgentAction(
                agent="tasks",
                action="list_items",
                result={
                    "items": [i.model_dump() for i in items],
                    "lists": [l.model_dump() for l in lists],
                },
            )
        
        elif any(word in message_lower for word in ["create list", "new list"]):
            from ..task_agent.models import CreateListRequest
            
            list_name = "New List"
            for entity in intent.entities.get("items", []):
                list_name = entity
                break
            
            task_list = self.task_agent.create_list(CreateListRequest(
                user_id=request.user_id,
                name=list_name,
            ))
            
            return AgentAction(
                agent="tasks",
                action="create_list",
                result={"list": task_list.model_dump()},
            )
        
        else:
            return AgentAction(
                agent="tasks",
                action="general",
                result={"message": "I can help you manage your tasks and shopping lists."},
            )

    def _handle_deals(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle deal-finding requests."""
        message_lower = request.message.lower()
        
        if any(word in message_lower for word in ["find", "search", "look for"]):
            query = None
            for item in intent.entities.get("items", []):
                query = item
                break
            
            from ..deal_finder_agent.models import SearchDealsRequest
            result = self.deal_finder.search_deals(SearchDealsRequest(
                user_id=request.user_id,
                query=query,
            ))
            
            return AgentAction(
                agent="deals",
                action="search_deals",
                result={"deals": [d.model_dump() for d in result.deals], "query": result.query_used},
            )
        
        elif any(word in message_lower for word in ["wishlist", "track", "alert"]):
            wishlist = self.deal_finder.get_wishlist(request.user_id)
            
            return AgentAction(
                agent="deals",
                action="get_wishlist",
                result={"wishlist": [w.model_dump() for w in wishlist]},
            )
        
        elif any(word in message_lower for word in ["recommend", "personalized", "for me"]):
            deals = self.deal_finder.get_personalized_deals(request.user_id)
            
            return AgentAction(
                agent="deals",
                action="personalized_deals",
                result={"deals": [d.model_dump() for d in deals]},
            )
        
        else:
            return AgentAction(
                agent="deals",
                action="general",
                result={"message": "I can help you find deals and track prices."},
            )

    def _handle_reservations(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle reservation requests."""
        message_lower = request.message.lower()
        
        if any(word in message_lower for word in ["book", "reserve", "make a reservation"]):
            result = self.reservation_agent.create_reservation_from_text(
                user_id=request.user_id,
                text=request.message,
            )
            
            return AgentAction(
                agent="reservations",
                action="create_reservation",
                result=result,
            )
        
        elif any(word in message_lower for word in ["recommend", "suggest", "find"]):
            if "restaurant" in message_lower or "dinner" in message_lower or "eat" in message_lower:
                venues = self.reservation_agent.recommend_restaurants(request.user_id)
                return AgentAction(
                    agent="reservations",
                    action="recommend_restaurants",
                    result={"venues": [v.model_dump() for v in venues]},
                )
        
        elif any(word in message_lower for word in ["upcoming", "my reservations", "booked"]):
            reservations = self.reservation_agent.get_upcoming_reservations(request.user_id)
            
            return AgentAction(
                agent="reservations",
                action="list_reservations",
                result={"reservations": [r.model_dump() for r in reservations]},
            )
        
        return AgentAction(
            agent="reservations",
            action="general",
            result={"message": "I can help you make reservations and find restaurants."},
        )

    def _handle_documentation(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle documentation requests."""
        message_lower = request.message.lower()
        
        if any(word in message_lower for word in ["checklist", "check list"]):
            from ..doc_generator_agent.models import GenerateChecklistRequest
            checklist = self.doc_generator.generate_checklist(GenerateChecklistRequest(
                user_id=request.user_id,
                task=request.message,
                include_time_estimates="time" in message_lower,
            ))
            
            return AgentAction(
                agent="documentation",
                action="generate_checklist",
                result={"checklist": checklist.model_dump()},
            )
        
        elif any(word in message_lower for word in ["template"]):
            from ..doc_generator_agent.models import GenerateTemplateRequest
            template = self.doc_generator.generate_template(GenerateTemplateRequest(
                user_id=request.user_id,
                template_type="general",
                purpose=request.message,
            ))
            
            return AgentAction(
                agent="documentation",
                action="generate_template",
                result={"template": template.model_dump()},
            )
        
        elif any(word in message_lower for word in ["sop", "procedure", "process"]):
            from ..doc_generator_agent.models import SOPRequest
            sop = self.doc_generator.generate_sop(SOPRequest(
                user_id=request.user_id,
                process_name="Process",
                description=request.message,
            ))
            
            return AgentAction(
                agent="documentation",
                action="generate_sop",
                result={"document": sop.model_dump()},
            )
        
        else:
            from ..doc_generator_agent.models import GenerateDocRequest
            doc = self.doc_generator.generate_process_doc(GenerateDocRequest(
                user_id=request.user_id,
                doc_type="guide",
                topic=request.message,
            ))
            
            return AgentAction(
                agent="documentation",
                action="generate_doc",
                result={"document": doc.model_dump()},
            )

    def _handle_profile(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle profile update requests."""
        profile_agent = self._get_profile_agent(request.user_id)
        
        from ..user_profile_agent.models import LearnFromTextRequest
        result = profile_agent.learn_from_text(LearnFromTextRequest(
            user_id=request.user_id,
            text=request.message,
            source="direct_input",
            auto_apply=True,
        ))
        
        return AgentAction(
            agent="profile",
            action="update_profile",
            result={
                "extracted": [p.model_dump() for p in result.extracted],
                "applied": [p.model_dump() for p in result.applied],
                "pending": [p.model_dump() for p in result.pending_confirmation],
            },
        )

    def _handle_general(self, request: OrchestratorRequest, intent: Intent) -> AgentAction:
        """Handle general/unclear requests."""
        profile_agent = self._get_profile_agent(request.user_id)
        profile_summary = profile_agent.get_profile_summary()
        
        response = self.llm.complete(
            f"You are an expert personal assistant. "
            f"User profile: {profile_summary}\n\n"
            f"User says: {request.message}\n\n"
            f"Respond helpfully:",
            temperature=0.7,
        )
        
        return AgentAction(
            agent="general",
            action="conversation",
            result={"response": response},
        )

    def _check_for_profile_updates(self, request: OrchestratorRequest) -> List[Dict[str, Any]]:
        """Check if the message contains profile-worthy information."""
        profile_agent = self._get_profile_agent(request.user_id)
        
        extraction = profile_agent.extract_preferences(request.message)
        
        high_confidence = [
            p for p in extraction.extracted_info
            if p.confidence >= 0.8
        ]
        
        for pref in high_confidence:
            profile_agent._apply_preference(pref)
        
        return [p.model_dump() for p in high_confidence]

    def _generate_response(
        self,
        request: OrchestratorRequest,
        intent: Intent,
        actions: List[AgentAction],
        results: Dict[str, Any],
    ) -> OrchestratorResponse:
        """Generate a natural language response."""
        profile_agent = self._get_profile_agent(request.user_id)
        profile_summary = profile_agent.get_profile_summary()
        
        actions_text = "\n".join(
            f"- {a.agent}: {a.action} ({'success' if a.success else 'failed'})"
            for a in actions
        )
        
        prompt = RESPONSE_GENERATION_PROMPT.format(
            message=request.message,
            intent=intent.primary,
            actions=actions_text,
            results=str(results)[:2000],
            profile_summary=profile_summary,
        )
        
        try:
            data = self.llm.complete_json(prompt, temperature=0.4)
            response_message = data.get("message", "I've processed your request.")
            suggestions = data.get("follow_up_suggestions", [])
        except Exception as e:
            logger.warning("Response generation failed: %s", e)
            
            if actions and actions[0].result:
                if "message" in actions[0].result:
                    response_message = actions[0].result["message"]
                elif "response" in actions[0].result:
                    response_message = actions[0].result["response"]
                else:
                    response_message = "I've processed your request."
            else:
                response_message = "I've processed your request."
            suggestions = []
        
        return OrchestratorResponse(
            message=response_message,
            intent=intent,
            actions_taken=[f"{a.agent}:{a.action}" for a in actions],
            data=results,
            follow_up_suggestions=suggestions,
        )

    def handle_request(
        self,
        request: AssistantRequest,
        job_updater: Optional[Callable[..., bool]] = None,
    ) -> AssistantResponse:
        """
        Handle a request using the AssistantRequest/Response models.
        
        This is an alternative entry point using the shared models.
        
        Args:
            request: The assistant request
            job_updater: Optional callback for async job status updates.
                         Signature: job_updater(status_text, progress, request_type) -> bool
                         Returns False if job was cancelled.
        """
        def _update(
            status_text: Optional[str] = None,
            progress: Optional[int] = None,
            request_type: Optional[str] = None,
        ) -> bool:
            if job_updater:
                return job_updater(
                    status_text=status_text,
                    progress=progress,
                    request_type=request_type,
                )
            return True

        orch_request = OrchestratorRequest(
            user_id=request.user_id,
            message=request.message,
            context=request.context,
        )

        if not _update(status_text="Classifying intent...", progress=5):
            return AssistantResponse(
                request_id=request.request_id,
                message="Request was cancelled.",
                actions_taken=["cancelled"],
                data={},
            )

        intent = self.classify_intent(orch_request.message)
        logger.info("Classified intent: %s (confidence: %.2f)", intent.primary, intent.confidence)

        if not _update(
            status_text=f"Processing {intent.primary} request...",
            progress=15,
            request_type=intent.primary,
        ):
            return AssistantResponse(
                request_id=request.request_id,
                message="Request was cancelled.",
                actions_taken=["cancelled"],
                data={},
            )

        actions: List[AgentAction] = []
        results: Dict[str, Any] = {}

        try:
            if intent.primary == "email":
                _update(status_text="Handling email request...", progress=30)
                action_result = self._handle_email(orch_request, intent)
                actions.append(action_result)
                results["email"] = action_result.result

            elif intent.primary == "calendar":
                _update(status_text="Checking your calendar...", progress=30)
                action_result = self._handle_calendar(orch_request, intent)
                actions.append(action_result)
                results["calendar"] = action_result.result

            elif intent.primary == "tasks":
                _update(status_text="Managing your tasks...", progress=30)
                action_result = self._handle_tasks(orch_request, intent)
                actions.append(action_result)
                results["tasks"] = action_result.result

            elif intent.primary == "deals":
                _update(status_text="Searching for deals...", progress=30)
                action_result = self._handle_deals(orch_request, intent)
                actions.append(action_result)
                results["deals"] = action_result.result

            elif intent.primary == "reservations":
                _update(status_text="Processing reservation request...", progress=30)
                action_result = self._handle_reservations(orch_request, intent)
                actions.append(action_result)
                results["reservations"] = action_result.result

            elif intent.primary == "documentation":
                _update(status_text="Generating documentation...", progress=30)
                action_result = self._handle_documentation(orch_request, intent)
                actions.append(action_result)
                results["documentation"] = action_result.result

            elif intent.primary == "profile":
                _update(status_text="Updating your profile...", progress=30)
                action_result = self._handle_profile(orch_request, intent)
                actions.append(action_result)
                results["profile"] = action_result.result

            else:
                _update(status_text="Processing your request...", progress=30)
                action_result = self._handle_general(orch_request, intent)
                actions.append(action_result)
                results["general"] = action_result.result

        except Exception as e:
            logger.error("Error handling request: %s", e)
            actions.append(AgentAction(
                agent="orchestrator",
                action="error",
                result={"error": str(e)},
                success=False,
            ))

        _update(status_text="Checking for profile updates...", progress=70)
        profile_updates = self._check_for_profile_updates(orch_request)

        _update(status_text="Generating response...", progress=85)
        response = self._generate_response(orch_request, intent, actions, results)
        response.profile_updates = profile_updates

        _update(status_text="Request completed", progress=100)

        return AssistantResponse(
            request_id=request.request_id,
            message=response.message,
            actions_taken=response.actions_taken,
            data=response.data,
            follow_up_suggestions=response.follow_up_suggestions,
        )
