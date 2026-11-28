"""Construct a worker request from the current owner's saved mission unit."""

from pathlib import Path

from chef.guards import mission
from chef.infrastructure import configuration
from chef.mission import managed_plans, unit_scope
from chef.mission.contracts import UNIT_HEADER, UNITS_FILE
from chef.mission.storage import read_table, require_unit
from chef.workers import controller
from chef.workers.review_results import REVIEW_ROLES


def build(args):
    if not args.role or not args.runtime or not args.session or not args.request_id:
        raise ValueError("unit dispatch requires --role, --runtime, --session and --request-id")
    if args.stored_brief:
        raise ValueError("unit dispatch already uses the stored brief")
    located = configuration.locate(args.project, args.runtime)
    adapter = configuration.read_adapter(located.adapter_path, args.runtime)
    active = mission.read_active(mission.state_dir_of(located.project, adapter))
    request = {"project": located.project, "mission": active.id, "unit": args.unit, "role": args.role,
               "ownerRuntime": args.runtime, "ownerSession": args.session, "requestId": args.request_id,
               "useStoredBrief": True, "prompt": args.prompt or "",
               "hostRoles": args.host_role or [], "hostTools": args.host_tool or [],
               "requiredTools": args.required_tool or []}
    if args.previous:
        request["previous"] = args.previous
    _, owner = controller.ownership(request, brief=True)
    request["unitContract"] = owner["unitContract"]
    role = controller.role_name(args.role)
    stored_files = unit_scope.files(located.project, owner["brief"]["files"])
    if role not in controller.READ_ONLY and role != "verifier":
        if not stored_files:
            raise ValueError("unit files must be an existing file or a JSON array of exact relative file paths; "
                             "update the approved brief or use a request with explicit files")
        requested = controller.project_paths(located.project, args.file or stored_files, "files")
        if any(value not in stored_files for value in requested):
            raise ValueError("--file must select from the approved unit files")
        request["files"] = requested
    elif args.file:
        raise ValueError("--file is only for implementation workers")
    request["inputPaths"] = [*stored_files, *(args.input_path or [])]
    if role in REVIEW_ROLES:
        request.update(reviewKind=args.review_kind or "unit", reviewReceipt=args.receipt)
    elif args.receipt or args.review_kind:
        raise ValueError("--receipt and --review-kind require a review role")
    if role == "verifier":
        folder = Path(owner["state"]) / owner["mission"]
        row = require_unit(read_table(folder / UNITS_FILE, UNIT_HEADER), args.unit)
        plan_path = managed_plans.path_for(folder, row)
        request["verificationPlan"] = plan_path
        request["inputPaths"].extend(controller.read_json(plan_path)["inputs"])
        request["prompt"] += (
            f"\nRun the bound plan with chef verify {args.unit} --runtime {args.runtime} "
            f'--plan "{plan_path}". Return the resulting receipt path.'
        )
    return request
