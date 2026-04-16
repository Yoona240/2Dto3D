"""
Validation utilities for enforcing fail-loud behavior.

This module provides centralized validation functions that raise explicit exceptions
with descriptive error messages when expected data is missing or invalid, rather than
using fallback defaults or silent conversions.

Follows the "Fail Loudly" principle from AGENTS.md:
- Configuration即契约: All parameters must be explicitly defined
- 无静默降级: Missing configuration or errors must immediately raise exceptions
- 单一定义源: Default values only in config.yaml, not in code
"""

from typing import Any, Dict, List, Optional


def require_param(params: dict, key: str, param_type: str = "request") -> Any:
    """
    Require a parameter to exist in dictionary, fail loudly if missing.
    
    Args:
        params: Dictionary containing parameters
        key: Required parameter key
        param_type: Type of parameter for error message (e.g., "request", "task", "gen3d task")
    
    Returns:
        The parameter value if present
    
    Raises:
        ValueError: If the required parameter is missing
    
    Example:
        >>> params = {"provider": "tripo", "image_id": "123"}
        >>> provider = require_param(params, "provider", "gen3d task")
        >>> # Returns "tripo"
        
        >>> params = {"image_id": "123"}
        >>> provider = require_param(params, "provider", "gen3d task")
        >>> # Raises ValueError: Missing required gen3d task parameter: 'provider'
    """
    if key not in params:
        raise ValueError(f"Missing required {param_type} parameter: '{key}'")
    return params[key]


def require_api_field(response: dict, field: str, api_name: str) -> Any:
    """
    Require a field to exist in API response, fail loudly if missing.
    
    Args:
        response: API response dictionary
        field: Required field name
        api_name: Name of the API for error message (e.g., "Rodin", "Tripo")
    
    Returns:
        The field value if present
    
    Raises:
        ValueError: If the required field is missing, includes full response for debugging
    
    Example:
        >>> response = {"status": "completed", "task_id": "123"}
        >>> status = require_api_field(response, "status", "Rodin")
        >>> # Returns "completed"
        
        >>> response = {"task_id": "123"}
        >>> status = require_api_field(response, "status", "Rodin")
        >>> # Raises ValueError: Rodin API response missing required field 'status'. Response: {...}
    """
    if field not in response:
        raise ValueError(
            f"{api_name} API response missing required field '{field}'. "
            f"Response: {response}"
        )
    return response[field]


def require_non_empty(value: Optional[str], context: str) -> str:
    """
    Require a string value to be non-empty, fail loudly if empty or None.
    
    Args:
        value: String value to validate
        context: Context description for error message (e.g., "LLM response text")
    
    Returns:
        The validated string value
    
    Raises:
        ValueError: If the value is None or empty string
    
    Example:
        >>> text = "Hello world"
        >>> validated = require_non_empty(text, "LLM response text")
        >>> # Returns "Hello world"
        
        >>> text = None
        >>> validated = require_non_empty(text, "LLM response text")
        >>> # Raises ValueError: LLM response text is empty or None
        
        >>> text = ""
        >>> validated = require_non_empty(text, "LLM response text")
        >>> # Raises ValueError: LLM response text is empty or None
    """
    if value is None or (isinstance(value, str) and value == ""):
        raise ValueError(f"{context} is empty or None")
    return value


def validate_response_structure(
    response: dict, 
    required_fields: List[str], 
    api_name: str
) -> None:
    """
    Validate multiple required fields exist in API response.
    
    Args:
        response: API response dictionary
        required_fields: List of required field names
        api_name: Name of the API for error message
    
    Raises:
        ValueError: If any required fields are missing, lists all missing fields
    
    Example:
        >>> response = {"status": "completed", "task_id": "123", "result": "success"}
        >>> validate_response_structure(response, ["status", "task_id"], "Rodin")
        >>> # No exception, all fields present
        
        >>> response = {"task_id": "123"}
        >>> validate_response_structure(response, ["status", "task_id", "result"], "Rodin")
        >>> # Raises ValueError: Rodin API response missing required fields: ['status', 'result']. Response: {...}
    """
    missing = [f for f in required_fields if f not in response]
    if missing:
        raise ValueError(
            f"{api_name} API response missing required fields: {missing}. "
            f"Response: {response}"
        )
