import base64
import csv
import io
import json
import logging
from random import randint

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import models
from django.db.models import Q, QuerySet, Count, Sum, Model
from django.db.models import CharField, TextField
from django.db.models import Avg, Max, Min
from django.utils.module_loading import import_string
from mcp.types import EmbeddedResource, TextResourceContents, BlobResourceContents, TextContent
from rest_framework.renderers import BaseRenderer

logger = logging.getLogger(__name__)

"""
    Tools to generate MongoDB-style $jsonSchema from Django models
    and to apply JSON-like queries to Django QuerySets using a subset 
    of MangoDB aggregation pipeline syntax.
"""


def generate_json_schema(model, fields=None, exclude=None):
    """
    Generate a MongoDB-style $jsonSchema from a Django model.

    Args:
        model: Django model class
        fields: Optional list of field names to include
        exclude: Optional list of field names to exclude

    Returns:
        A dict representing the $jsonSchema
    """

    type_mapping = {
        models.CharField: "string",
        models.TextField: "string",
        models.IntegerField: "int",
        models.FloatField: "double",
        models.BooleanField: "bool",
        models.DateTimeField: "date",
        models.DateField: "date",
        models.TimeField: "string",
        models.EmailField: "string",
        models.URLField: "string",
        models.DecimalField: "double",
        models.AutoField: "int",
        models.BigAutoField: "long",
        models.BigIntegerField: "long",
        models.JSONField: "object",
    }

    schema = {
        "description": (model.__doc__ or "").strip(),
        "$jsonSchema": {
            "bsonType": "object",
            "properties": {},
            "required": []
        }
    }

    for field in model._meta.get_fields():
        if not field.concrete:
            continue
        if fields and field.name not in fields:
            continue
        if exclude and field.name in exclude:
            continue

        prop = {}

        # Primary key description
        if getattr(field, 'primary_key', False):
            prop["description"] = "Primary unique identifier for this model"

        # ForeignKey
        if isinstance(field, models.ForeignKey):
            prop["bsonType"] = "objectId"
            prop["description"] = f"Reference to {field.related_model.__name__}"
            if field.help_text:
                prop["description"] += ": " + str(field.help_text)
            prop["ref"] = field.related_model.__name__
        else:
            # Type detection
            for django_type, bson_type in type_mapping.items():
                if isinstance(field, django_type):
                    prop["bsonType"] = bson_type
                    break
            else:
                prop["bsonType"] = "string"

            # Regular field description
            if field.help_text:
                prop["description"] = field.help_text
            if field.choices:
                # Add enum values
                prop["enum"] = [choice[0] for choice in field.choices]

                # Build display labels
                choice_desc = ", ".join(f"{repr(val)} = {label}" for val, label in field.choices)

                # Append to existing or new description
                if "description" in prop:
                    prop["description"] += f" Choices: {choice_desc}"
                else:
                    prop["description"] = f"Choices: {choice_desc}"

        schema["$jsonSchema"]["properties"][field.name] = prop

        if not getattr(field, 'null', True) and not getattr(field, 'blank', True):
            schema["$jsonSchema"]["required"].append(field.name)

    if not schema["$jsonSchema"]["required"]:
        del schema["$jsonSchema"]["required"]

    return schema



PIPELINE_DSL_SPEC="""
The syntax to query is a subset of MangoDB aggregation pipeline JSON with support of following stages : 

1. $lookup: Joins another collection :.
  - "from" must refer to a model name listed in ref in the schema (if defined).
  - "localField" must be a field path on the base collection or a previous $lookup alias.
  - "foreignField" must be "_id"
  - "as" defines an alias used in subsequent $match and $lookup stages as a prefix (e.g., alias.field).
2. $match: Filter documents using comparison and logical operators.
  - Supports: $eq, $ne, $gt, $gte, $lt, $lte, $in, $nin, $regex in addition to $text for collections that support full text search.
  - Field references can include lookup aliases via dot notation, e.g. "user.name"
3. $sort: Sorts the result. Keys must map to model fields.
4. $limit: Truncates the result set to the specified number of items.
5. $project: Selects specific fields for results. Only "flat" objects are supported.
   Value is either a number/boolean to include/exclude the field or a string starting in format 
   "$<lookupAlias>.<field>" to project a field from a previous $lookup stage.  
6. $search: For collection that support full-text search. Limited to {"text":{"query":"<keyword>"}}.
7. $group: Groups the result set by a field and applies aggregations.
     - It must be the **final** stage in the pipeline.
     - You cannot have a $project stage in the pipeline.
     - `_id` can be null for global aggregation or a $<field> reference of a single field or lookup field or an object mapping "keys" to "$<field>" refs.
     - Supported accumulator operators: `$sum`, `$avg`, `$min`, `$max` and `$count`

All other stages NOT SUPPORTED : $addFields, $set, $unset, $unwind ...
"""

def apply_json_mango_query(queryset: QuerySet, pipeline: list[dict],
                           allowed_models: list = None, extended_operators: list = None,
                           text_search_fields: list | str = '*'):
    """
    Apply a JSON-like query to a Django QuerySet using a subset of MangoDB aggregation pipeline syntax.
    see pipeline_dsl_spec() for details.
    :param queryset: The base queryset to query
    :param pipeline: a list of stages to apply to the queryset compliant with pipeline_dsl_spec()
    :param allowed_models: List of allowed models for $lookup stages. If None, all models are allowed. Can be the string name or the Model class.
    :param extended_operators: List of Queryset API lookups to support as exetended operators. this interprets {"<field>":{"$<op>": value} as Q({field}__{op}=value)
    :param text_search_fields: List of field names to apply `$text` full-text search to. Use "*" to apply to all CharField and TextField fields of the model. Required if `$text` is used.
    :return: an iterable (eventually the queryset) of JSON results.
    """

    if extended_operators is None:
        extended_operators = []

    if allowed_models:
        allowed_models = [model.lower() if isinstance(model, str) else model._meta.model_name.lower()  for model in allowed_models]

    model = queryset.model
    if text_search_fields == "*":
        text_search_fields = [f.name for f in model._meta.get_fields() if
                              isinstance(f, (CharField, TextField)) and f.concrete and not f.is_relation]

    lookup_alias_map = {}

    # First pass: Apply lookups
    for stage in pipeline:
        if "$lookup" in stage:
            lookup = stage["$lookup"]
            _validate_lookup(model, lookup, allowed_models, lookup_alias_map)
            as_field = lookup["as"]
            local_field = _translate_field(lookup["localField"], lookup_alias_map)
            foreign_field = lookup["foreignField"]
            lookup_alias_map[as_field] = {
                "prefix": local_field.replace("_id", ""),
                "foreign_field": foreign_field
            }

    # Second pass: Apply rest
    projection_fields = None
    projection_mapping = None
    skip_value = None

    for i, stage in enumerate(pipeline):
        if "$match" in stage:
            match_stage = stage["$match"]
            if "$text" in match_stage:
                if not text_search_fields:
                    raise ValueError("$text used but full text search is not supported for this collection.")
                search_value = match_stage["$text"].get("$search", "")
                del match_stage["$text"]
                q = _build_text_search_q(search_value, text_search_fields)
                if match_stage:
                    q &= _parse_match(match_stage, extended_operators, lookup_alias_map, text_search_fields)
                queryset = queryset.filter(q)
            else:
                queryset = queryset.filter(_parse_match(stage["$match"], extended_operators, lookup_alias_map, text_search_fields=[]))

        elif "$search" in stage:
            search = stage["$search"]
            if not text_search_fields:
                raise ValueError("$search used but full text search is not supported for thsi collection.")
            search_value = search["text"]["query"]
            path = search["text"].get("path", text_search_fields)
            search_fields = [path] if isinstance(path, str) else path

            if not all(f in text_search_fields for f in search_fields):
                raise ValueError("$search path contains fields that are not allowed for search")
            q = _build_text_search_q(search_value, search_fields)
            queryset = queryset.filter(q)

        elif "$sort" in stage:
            order = []
            for field, direction in stage["$sort"].items():
                order.append(field if direction == 1 else f"-{field}")
            queryset = queryset.order_by(*order)

        elif "$skip" in stage:
            skip_value = stage["$skip"]

        elif "$limit" in stage:
            queryset = queryset[:stage["$limit"]]

        elif "$project" in stage:
            if any("$group" in s for s in pipeline[i+1:]):
                raise ValueError("$project cannot appear when pipeline contains $group :"
                                 " please review pipeline syntax constriants.")
            projection_fields, projection_mapping = _interpret_projection(stage["$project"], lookup_alias_map)

        elif "$group" in stage:
            if i != len(pipeline) - 1:
                raise ValueError("$group must be the last stage in the pipeline.:"
                                 " please review pipeline syntax constriants.")

            group = stage["$group"]
            group_id = group["_id"]
            annotations = {}

            for key, agg in group.items():
                if key == "_id":
                    continue
                if not isinstance(agg, dict) or len(agg) != 1:
                    raise ValueError(f'Aggregation for key {key} can only be a JSON object of format '+'{"$<operator>": "<parameter>"}.')
                op, arg = next(iter(agg.items()))
                if op == "$sum":
                    if arg == 1:
                        annotations[key] = Count("id")
                    elif isinstance(arg, str) and arg.startswith("$"):
                        annotations[key] = Sum(_translate_field(arg[1:], lookup_alias_map))
                    else:
                        raise ValueError("$sum only supports 1 or field references.")
                elif op == "$avg":
                    if isinstance(arg, str) and arg.startswith("$"):
                        annotations[key] = Avg(_translate_field(arg[1:], lookup_alias_map))
                    else:
                        raise ValueError("$avg requires a field reference.")
                elif op == "$min":
                    if isinstance(arg, str) and arg.startswith("$"):
                        annotations[key] = Min(_translate_field(arg[1:], lookup_alias_map))
                    else:
                        raise ValueError("$min requires a field reference.")
                elif op == "$max":
                    if isinstance(arg, str) and arg.startswith("$"):
                        annotations[key] = Max(_translate_field(arg[1:], lookup_alias_map))
                    else:
                        raise ValueError("$max requires a field reference.")

                elif op == "$count":
                    if arg == 1:
                        annotations[key] = Count("id")
                    elif isinstance(arg, str) and arg.startswith("$"):
                        annotations[key] = Count(_translate_field(arg[1:], lookup_alias_map))
                    else:
                        raise ValueError("$count only supports value 1 or a field reference.")
                else:
                    raise ValueError(f"Unsupported aggregation operator: {op}")

            if group_id is None:
                return [queryset.aggregate(**annotations)]
            elif isinstance(group_id, str) and group_id.startswith("$"):
                group_field = _translate_field(group_id[1:], lookup_alias_map)
                mapping = dict((k,k) for k in group.keys() if k != "_id")
                mapping[group_id[1:]] = group_field

                return _postprocess_projection(queryset.values(group_field).annotate(**annotations), mapping)
            elif isinstance(group_id, dict):
                mapping = dict((k,k) for k in group.keys() if k != "_id")
                group_fields = set()
                for key, formula in group_id.items():
                    if not isinstance(formula, str) or not formula.startswith("$"):
                        raise ValueError(f"_id in $group only supports null, string or one level object: value of {key} must be a string reference to field like $<field>.")
                    group_field = _translate_field(formula[1:], lookup_alias_map)
                    mapping["_id."+key] = group_field
                    group_fields.add(group_field)
                return _postprocess_projection(queryset.values(*group_fields).annotate(**annotations), mapping)
            else:
                raise ValueError("Unsupported _id value in $group. Only allowed : null a \"$field\" or a {\"key\":\"$field\",...} object.")

        elif "$lookup" in stage:
            continue

        else:
            raise ValueError(f"Unsupported stage {stage} : please review pipeline syntax constraints")

    if skip_value is not None:
        queryset = queryset[skip_value:]

    if projection_fields:
        queryset = queryset.values(*projection_fields)
        return _postprocess_projection(queryset, projection_mapping)

    return _postprocess_projection(queryset.values(), None)


def _interpret_projection(projection, lookup_map):
    fields = []
    mapping = {}
    for output_field, spec in projection.items():
        if isinstance(spec, str) and spec.startswith("$"):
            path = spec[1:]
            if path == "_id":
                path = "pk"
            internal_field = _translate_field(path, lookup_map)
            fields.append(internal_field)
            mapping[output_field] = internal_field
        elif spec:
            path = output_field if output_field != "_id" else "pk"
            internal_field = _translate_field(path, lookup_map)
            fields.append(internal_field)
            mapping[output_field] = internal_field
    return fields, mapping


def _postprocess_projection(queryset, projection_mapping):
    if not projection_mapping:
        yield from queryset
        return

    for row in queryset:
        result = {}
        for key, internal_key in projection_mapping.items():
            value = row.get(internal_key)
            _assign_nested_value(result, key.split("."), value)
        yield result


def _assign_nested_value(target, path_parts, value):
    for part in path_parts[:-1]:
        target = target.setdefault(part, {})
    target[path_parts[-1]] = value


def _restore_field_path(field, lookup_map):
    for alias, info in lookup_map.items():
        prefix = info['prefix']
        if field.startswith(prefix + "__"):
            return alias + "." + field[len(prefix + "__"):].replace("__", ".")
    return field.replace("__", ".")


def _validate_lookup(model, lookup, allowed_models, lookup_map):
    from_model_name = lookup["from"]
    if allowed_models is not None and from_model_name.lower() not in allowed_models:
        raise ValueError(f"Invalid lookup from collection '{from_model_name}'  : please reveiw schemas.")

    local_field_name = _translate_field(lookup["localField"], lookup_map)
    foreign_field_name = lookup["foreignField"]

    base_model, field_name = _resolve_model_from_path(model, local_field_name, lookup_map)

    try:
        local_field = base_model._meta.get_field(field_name)
    except Exception:
        raise ValueError(f"Invalid localField '{lookup['localField']}' : please review supported pipeline syntax and  schemas for {base_model._meta.model_name}.'.")

    if not local_field.is_relation or not local_field.many_to_one:
        raise ValueError(f"Invalid localField '{lookup['localField']}' : is not a reference, please review supported pipeline syntax and  schemas for {base_model._meta.model_name}.")

    related_model = local_field.related_model
    if foreign_field_name != related_model._meta.pk.name and foreign_field_name != "_id" and foreign_field_name != "pk":
        raise ValueError(f"Invalid foreignField '{lookup['foreignField']}': please review supported pipeline syntax and  schemas for {base_model._meta.model_name}.")


def _resolve_model_from_path(model, field_path, lookup_map):
    parts = field_path.split("__")
    current_model = model
    for part in parts[:-1]:
        try:
            field = current_model._meta.get_field(part)
            if field.is_relation:
                current_model = field.related_model
            else:
                break
        except Exception:
            raise ValueError(f"Invalid field path '{field_path}' at '{part}' in model '{current_model.__name__}'.")
    return current_model, parts[-1]


def _parse_match(match, extended_operators, lookup_map, text_search_fields=None):
    if "$and" in match:
        return Q(*[_parse_match(cond, extended_operators, lookup_map) for cond in match["$and"]])
    if "$or" in match:
        return Q(*[_parse_match(cond, extended_operators, lookup_map) for cond in match["$or"]], _connector=Q.OR)
    if "$nor" in match:
        return ~Q(*[_parse_match(cond, extended_operators, lookup_map) for cond in match["$nor"]], _connector=Q.OR)

    q = Q()
    for field, condition in match.items():
        field = _translate_field(field, lookup_map)

        if isinstance(condition, dict):
            for op, value in condition.items():
                if op.startswith("$"):
                    op_name = op[1:]
                    negate=False
                    if op_name == "ne":
                        negate = True
                        op_name = "eq"
                    elif op_name == "nin":
                        negate = True
                        op_name = "in"

                    if op_name == "eq" and value is None:
                        key = f"{field}__isnull"
                        value = True
                    elif op_name in ["eq", "gt", "gte", "lt", "lte", "in"]:
                        django_op = "" if op_name=="eq" else f"__{op_name}"
                        key = f"{field}{django_op}"
                    elif op_name == "regex":
                        key = f"{field}__regex"
                    elif op_name in extended_operators:
                        key = f"{field}__{op_name}"
                    else:
                        raise ValueError(f"Unsupported operator {op} : please reveiwe pipeline syntax constraints ")
                    q &= ~Q(**{key: value}) if negate else Q(**{key: value})
                else:
                    raise ValueError(f"Unknown match key: {op} : please reveiwe pipeline syntax constraints")
        else:
            q &= Q(**{field: condition})
    return q


def _translate_field(field, lookup_map):
    if field == "_id":
        return "pk"
    if "." in field:
        alias, rest = field.split(".", 1)
        if alias in lookup_map:
            return f"{lookup_map[alias]['prefix']}__{rest}"
        else:
            raise ValueError(f"Unknown lookup alias '{alias}', ensure it appears in the 'as' field of a previous $lookup")
    return field


def _build_text_search_q(search_value, fields):
    q = Q()
    for word in search_value.strip().split():
        word_q = Q()
        for f in fields:
            word_q |= Q(**{f"{f}__icontains": word})
        q &= word_q
    return q


class ModelQueryToolsetMeta(type):
    registry = {}

    def __init__(cls, name, bases, namespace):
        super().__init__(name, bases, namespace)
        if name != "ModelQueryToolset":
            ModelQueryToolsetMeta.registry[name] = cls


class ModelQueryToolset(metaclass=ModelQueryToolsetMeta):
    """
    Base class for models that can be queried using the MCP QueryTool.
    """

    mcp_server: 'DjangoMCP' = None
    "The server to use, if not set, the global one will be used."

    model: type(Model) = None
    " The model to query, this is used to generate the JSON schema and the query methods. "

    exclude_fields: list[str] = []
    """List of fields to exclude from the schema. Related fields to collections that are not published"
    in any other ModelQueryToolset of same server will be autoamtically excluded"""

    fields: list[str] = None
    "The list of fields to include"

    search_fields: list[str] = None
    "List of fields for full text search, if not set it defaults to textual fields allowed by 'fields' parameters."

    extra_filters: list[str] = None
    "A list of queryset api filters that will be accessible to the MCP client for querying."

    extra_instructions: str = None
    "Extra instruction to provide, for example on when to query this collection"

    output_format : str = "json"
    "Desired output format, the corresponding DRF renderer class must be registered in the DJANGO_MCP_OUTPUT_RENDERER_CLASSES setting. By default JSONRenderer is used."

    output_as_resource = False
    "When set to true the MCP result will return the result as an embedded resource"

    @classmethod
    def get_text_search_fields(cls):
        if hasattr(cls, "_effective_text_search_fields"):
            return cls._effective_text_search_fields
        if cls.search_fields is not None:
            cls._effective_text_search_fields = set(cls.search_fields)
        elif cls.fields is None:
            cls._effective_text_search_fields = set(f.name for f in cls.model._meta.get_fields() if
                                                    isinstance(f, (
                                                    CharField, TextField)) and f.concrete and not f.is_relation)
        else:
            cls._effective_text_search_fields = set(f.name for f in cls.model._meta.get_fields() if f.name in cls.fields and
                                                    isinstance(f, (CharField, TextField)) and f.concrete and not f.is_relation)
        if not cls._effective_text_search_fields:
            logger.debug(f"Full text search disabled for {cls.model}: no search fields resolved")
        else:
            logger.debug(
                f"Full text search for {cls.model} enabled on fields: {','.join(cls._effective_text_search_fields)}")
        return cls._effective_text_search_fields

    @classmethod
    def get_published_models(cls):
        if hasattr(cls, "_effective_published_models"):
            return cls._effective_published_models
        cls._effective_published_models = set(c.model for c in ModelQueryToolsetMeta.registry.values() if
                                              c.mcp_server == cls.mcp_server)
        return cls._effective_published_models

    @classmethod
    def get_excluded_fields(cls):
        if hasattr(cls, "_effective_excluded_fields"):
            return cls._effective_excluded_fields
        cls._effective_excluded_fields = set(cls.exclude_fields or [])
        published_models = cls.get_published_models()
        unpublished_fks = [f.name for f in cls.model._meta.get_fields()
                           if f.is_relation and f.related_model not in published_models]
        if unpublished_fks:
            logger.info(f"The following related fields of {cls.model} will not be published in {cls} "
                        f"because their models are not published: {unpublished_fks}")
            cls._effective_excluded_fields.update(
                unpublished_fks
            )
        return cls._effective_excluded_fields

    def get_queryset(self) -> QuerySet:
        """
        Returns the queryset to use for this toolset. This method can be overridden to filter the queryset
        based on the request or other parameters, the request is available in self.request and the MCP Server
        context in self.context
        """
        return self.model._default_manager.all()

    def __init__(self, context=None, request=None):
        self.context = context
        self.request = request


class _QueryExecutor:
    def __init__(self, query_tool_models, context=None, request=None):
        self.query_tool_models = query_tool_models
        self.context = context
        self.request = request

    def query(self, collection : str, search_pipeline: list[dict] = []):
        mql_model = self.query_tool_models.get(collection.lower())
        if not mql_model:
            raise ValueError(f"No such collection, available collections: {', '.join(self.query_tool_models.keys())}")
        instance = mql_model(self.context, self.request)
        qs = instance.get_queryset()

        ret = list(apply_json_mango_query(qs, search_pipeline,
                                           text_search_fields=instance.get_text_search_fields(),
                                           allowed_models=instance.get_published_models(),
                                           extended_operators=instance.extra_filters))

        if not ret:
            if instance.output_as_resource:
                return ["No results found"]
            else:
                return ret

        renderer = _output_formats.get(instance.output_format)

        assert renderer is not None, "Bad output format, but we were meant to have validated it at startup"

        if not isinstance(renderer, BaseRenderer):
            renderer = renderer()
        ret = renderer.render(ret)

        if instance.output_as_resource:
            if not ret: return ["No results found"]
            if (renderer.media_type.startswith("application/") and  "json" in renderer.media_type) \
                    or renderer.media_type.startswith("text/"):
                return ["Results attached", EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=f"resource://query_result/{renderer.format}",
                        mimeType=renderer.media_type,
                        text=ret
                    )
                )]
            else:
                return ["Results attached", EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=f"resource://query_result/{renderer.format}",
                        mimeType=renderer.media_type,
                        blob=base64.b64encode(ret).decode('utf-8')
                    )
                )]

        else:
            return [TextContent(type="text", text=ret)]



class QueryTool:
    def __init__(self):
        self.query_tool_models = {}

    def add_query_tool_model(self, cls):
        if cls.output_format not in _output_formats:
            raise ValueError(f"Output format {cls.output_format} is not supported, available formats: {', '.join(_output_formats.keys())}. Verify your DJANGO_MCP_OUTPUT_RENDERER_CLASSES setting.")
        self.query_tool_models[cls.model._meta.model_name.lower()] = cls

    def get_instructions(self):
        ret = f"""
Use this tool to query data available in the server. 
The `collection` parameter specifies the collection to query and the `search_pipeline` parameter is 
a list of stage of a MongoDB aggregation pipeline with restricted syntax.

## MongoDB aggregation pipeline syntax supported
{PIPELINE_DSL_SPEC}. 

## Available collections to query
"""
        for name, cls in self.query_tool_models.items():
            ret+=f"""
### '{name}' collection
Documents conform the following JSON Schema
```json
{generate_json_schema(cls.model, fields=cls.fields,
                      exclude=cls.get_excluded_fields())}
```

"""
            if cls.get_text_search_fields():
                ret += "Full text search is supported on the following fields: " + ", ".join(
                    cls.get_text_search_fields()) + "."
            else:
                ret += "Full text search is not supported on this collection."

            if cls.extra_instructions:
                ret += f"\n\nExtra instructions for this collection:\n\n{cls.extra_instructions}\n"
        return ret

    def executor_factory(self, context, request):
        return _QueryExecutor(self.query_tool_models, context, request)

    def _add_tools_to(self, tool_manager):
        from .djangomcp import _ToolsetMethodCaller
        # ITerate all the methods whose name does not start with _ and register them with mcp_server.add_tool
        ret = []
        def _dumb_query(collection : str, search_pipeline: list[dict] = []):
            ...

        name = "query_data_collections"

        tool = tool_manager.add_tool(
            fn=sync_to_async(_dumb_query),
            name=name,
            description=self.get_instructions()
        )

        tool.context_kwarg = "_context"
        tool.fn = _ToolsetMethodCaller(self.executor_factory, "query", "_context", False)
        return [tool]

_output_formats=None

def init(global_mcp_server : 'DjangoMCP'):
    global _output_formats
    renderers_classes = (import_string(renderer_class) for renderer_class in
                           getattr(settings, "DJANGO_MCP_OUTPUT_RENDERER_CLASSES", ["rest_framework.renderers.JSONRenderer"]))
    _output_formats = {renderer_class.format: renderer_class for renderer_class in renderers_classes}

    server_tools = {}
    for name, cls in ModelQueryToolsetMeta.registry.items():
        cls.mcp_server = cls.mcp_server or global_mcp_server
        querytool = server_tools.get(cls.mcp_server)
        if not querytool:
            querytool = QueryTool()
            server_tools[cls.mcp_server] = querytool

        querytool.add_query_tool_model(cls)

    for server, tool in server_tools.items():
        server.register_mcptoolset(tool)


