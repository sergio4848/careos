"""Import every model module so ``Base.metadata`` is complete (Alembic, tests)."""

from careos.db.base import Base
from careos.modules.ai_orchestrator import models as ai_models
from careos.modules.audit import models as audit_models
from careos.modules.device_gateway import models as gateway_models
from careos.modules.devices import models as device_models
from careos.modules.escalation_engine import models as escalation_models
from careos.modules.identity import models as identity_models
from careos.modules.incident_engine import models as incident_models
from careos.modules.notification_engine import models as notification_models
from careos.modules.organisations import models as organisation_models
from careos.modules.service_users import models as service_user_models

__all__ = [
    "Base",
    "ai_models",
    "audit_models",
    "device_models",
    "escalation_models",
    "gateway_models",
    "identity_models",
    "incident_models",
    "notification_models",
    "organisation_models",
    "service_user_models",
]

metadata = Base.metadata
