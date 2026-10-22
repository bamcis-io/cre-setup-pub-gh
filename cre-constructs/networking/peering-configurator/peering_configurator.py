"""CloudFormation custom resource: cross-region VPC peering configurator.

Creates + accepts + routes for every unique pair in a set of
{region, vpc_id, cidr, route_table_ids} descriptors (full mesh = N·(N-1)/2
connections).

Event shape (passed as ResourceProperties by the PeeringMesh construct):

  {
    "ManagedTag": { "Key": "CreDemo", "Value": "managed" },
    "Peers": [
      {
        "Role": "",            # optional, free-form; requester-preference only
        "Region": "us-east-1",
        "VpcId": "vpc-...",
        "VpcCidr": "10.0.0.0/16",
        "RouteTableIds": ["rtb-...", "rtb-..."]
      },
      ...
    ]
  }

On Create / Update: creates every unique peering pair (reusing a live one found in
either direction, and accepting it if a previous run left it pending), accepts in
the peer region, and points the route for the other side's CIDR at it in every
supplied route table (both directions) — replacing an existing route, which may be
a blackhole to a since-deleted peering. Update also tears down pairs that the new
Peers no longer contain.

On Delete: removes the routes that target each managed peering, then deletes the
peerings tagged with the managed tag. Routes do NOT go away with the peering — EC2
leaves them as blackholes — so they are removed explicitly.

Idempotent: tags every peering with the managed tag (default CreDemo=managed)
so re-runs discover existing connections instead of duplicating.

Notes:
  - The managed tag is configurable: read from ResourceProperties.ManagedTag,
    defaulting to {"Key": "CreDemo", "Value": "managed"}.
  - The peer Role is optional / free-form; it only affects which peer is the
    requester in a pair (cosmetic). An empty role falls through to array order.

Pure stdlib + boto3 + urllib3. Python 3.12.
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
import urllib3

HTTP = urllib3.PoolManager()

DEFAULT_MANAGED_TAG = {"Key": "CreDemo", "Value": "managed"}


# ---------------------------------------------------------------------------
# CloudFormation response helper
# ---------------------------------------------------------------------------

def _send(response_url: str, event: Dict[str, Any], context: Any,
          status: str, reason: str = "OK", data: Optional[Dict[str, Any]] = None) -> None:
    physical_id = event.get("PhysicalResourceId") \
        or f"{context.function_name}-{event.get('LogicalResourceId', '')}"
    body = json.dumps({
        "Status": status,
        "Reason": reason,
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "PhysicalResourceId": physical_id,
        "Data": data or {},
    })
    try:
        HTTP.request(
            "PUT", response_url,
            headers={"content-type": "", "content-length": str(len(body))},
            body=body,
        )
    except Exception as exc:  # noqa: BLE001 — we want any failure logged
        print(f"Failed to send CFN response: {exc}")
        raise


# ---------------------------------------------------------------------------
# Managed tag (configurable; default CreDemo=managed)
# ---------------------------------------------------------------------------

def _managed_tag(props: Dict[str, Any]) -> Tuple[str, str]:
    """Return (key, value) for the managed tag from ResourceProperties.ManagedTag."""
    tag = props.get("ManagedTag") or DEFAULT_MANAGED_TAG
    return tag.get("Key", DEFAULT_MANAGED_TAG["Key"]), tag.get("Value", DEFAULT_MANAGED_TAG["Value"])


# ---------------------------------------------------------------------------
# Core peering logic
# ---------------------------------------------------------------------------

def _ec2(region: str):
    return boto3.client("ec2", region_name=region)


# Peerings in these states exist (or are about to). Anything else — deleted,
# rejected, expired, failed — is history and must not be reused or deleted again.
LIVE_STATES = ["initiating-request", "pending-acceptance", "provisioning", "active"]


def _find_peerings(a: Dict[str, Any], b: Dict[str, Any],
                   tag: Optional[Tuple[str, str]] = None) -> List[Dict[str, Any]]:
    """Live peerings between the two VPCs, in EITHER direction.

    One direction is not enough: a change of Role or peer order flips which side is
    the requester, and EC2 rejects a second peering between the same two VPCs, so a
    one-way lookup turned that harmless reorder into a failed Update. Each result
    carries `_accepter` (the peer descriptor on the accepting side), because only that
    region can accept a pending peering.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for req, acc in ((a, b), (b, a)):
        filters = [
            {"Name": "requester-vpc-info.vpc-id", "Values": [req["VpcId"]]},
            {"Name": "accepter-vpc-info.vpc-id", "Values": [acc["VpcId"]]},
            {"Name": "status-code", "Values": LIVE_STATES},
        ]
        if tag:
            filters.append({"Name": f"tag:{tag[0]}", "Values": [tag[1]]})
        resp = _ec2(req["Region"]).describe_vpc_peering_connections(Filters=filters)
        for pcx in resp.get("VpcPeeringConnections", []):
            found[pcx["VpcPeeringConnectionId"]] = {**pcx, "_accepter": acc}
    return list(found.values())


def _accept(peering_id: str, accepter: Dict[str, Any], tag_key: str, tag_value: str) -> None:
    ec2 = _ec2(accepter["Region"])
    ec2.accept_vpc_peering_connection(VpcPeeringConnectionId=peering_id)
    # Also tag on the accepter side for easier discovery / cleanup.
    ec2.create_tags(Resources=[peering_id], Tags=[{"Key": tag_key, "Value": tag_value}])
    print(f"Accepted peering {peering_id} in {accepter['Region']}")


def _create_peering(src: Dict[str, Any], dst: Dict[str, Any],
                    tag_key: str, tag_value: str) -> str:
    """Create a peering connection from src→dst, tag it, and accept in dst's region."""
    existing = _find_peerings(src, dst)
    if existing:
        pcx = existing[0]
        peering_id = pcx["VpcPeeringConnectionId"]
        status = pcx.get("Status", {}).get("Code")
        print(f"Peering {src['Region']}↔{dst['Region']} already exists: {peering_id} ({status})")
        # A run that died between create and accept (Lambda timeout, async retry)
        # leaves it pending. Returning it as-is reported SUCCESS on a peering that
        # never carries traffic and expires after 7 days.
        if status == "pending-acceptance":
            _accept(peering_id, pcx["_accepter"], tag_key, tag_value)
        return peering_id

    requester = _ec2(src["Region"])
    resp = requester.create_vpc_peering_connection(
        VpcId=src["VpcId"],
        PeerVpcId=dst["VpcId"],
        PeerRegion=dst["Region"],
        TagSpecifications=[{
            "ResourceType": "vpc-peering-connection",
            "Tags": [
                {"Key": tag_key, "Value": tag_value},
                {"Key": "Name", "Value": f"{src['Region']}-{dst['Region']}"},
            ],
        }],
    )
    peering_id = resp["VpcPeeringConnection"]["VpcPeeringConnectionId"]
    print(f"Requested peering {src['Region']}→{dst['Region']}: {peering_id}")

    # Accept in the peer region (peering may take a moment to become visible)
    waiter = _ec2(dst["Region"]).get_waiter("vpc_peering_connection_exists")
    waiter.wait(VpcPeeringConnectionIds=[peering_id], WaiterConfig={"Delay": 5, "MaxAttempts": 24})
    _accept(peering_id, dst, tag_key, tag_value)
    return peering_id


def _error_code(exc: Exception) -> Optional[str]:
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


def _add_routes(region: str, route_table_ids: Iterable[str],
                destination_cidr: str, peering_id: str) -> None:
    """Point each table's route for destination_cidr at peering_id (safe to re-run)."""
    ec2 = _ec2(region)
    for rtb in route_table_ids:
        try:
            ec2.create_route(
                RouteTableId=rtb,
                DestinationCidrBlock=destination_cidr,
                VpcPeeringConnectionId=peering_id,
            )
            print(f"Route added: {rtb} {destination_cidr} → {peering_id}")
        except ec2.exceptions.ClientError as exc:
            if _error_code(exc) != "RouteAlreadyExists":
                raise
            # The existing route may target a peering that no longer exists — a
            # blackhole left by a teardown + redeploy or a rollback. Treating it as
            # done reported SUCCESS with cross-region traffic dead, so repoint it.
            ec2.replace_route(
                RouteTableId=rtb,
                DestinationCidrBlock=destination_cidr,
                VpcPeeringConnectionId=peering_id,
            )
            print(f"Route replaced: {rtb} {destination_cidr} → {peering_id}")


def _delete_routes(region: str, route_table_ids: Iterable[str],
                   destination_cidr: str, peering_id: str) -> None:
    """Remove routes for destination_cidr that target peering_id; leave any other route alone."""
    ec2 = _ec2(region)
    for rtb in route_table_ids:
        try:
            tables = ec2.describe_route_tables(RouteTableIds=[rtb]).get("RouteTables", [])
        except ec2.exceptions.ClientError as exc:
            if _error_code(exc) == "InvalidRouteTableID.NotFound":
                continue  # that region's stack is already gone
            raise
        for table in tables:
            for route in table.get("Routes", []):
                if (route.get("DestinationCidrBlock") == destination_cidr
                        and route.get("VpcPeeringConnectionId") == peering_id):
                    try:
                        ec2.delete_route(RouteTableId=rtb, DestinationCidrBlock=destination_cidr)
                        print(f"Route deleted: {rtb} {destination_cidr} → {peering_id}")
                    except ec2.exceptions.ClientError as exc:
                        if _error_code(exc) != "InvalidRoute.NotFound":
                            raise


def _unique_pairs(peers: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """All unique unordered pairs (full mesh).

    A peer whose free-form Role is "Client" is preferred as the requester (tidier;
    client-initiated peerings). With empty/any role this falls through to array
    order — correct for the generic N-region case.
    """
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for i in range(len(peers)):
        for j in range(i + 1, len(peers)):
            a, b = peers[i], peers[j]
            if a.get("Role") == "Client":
                pairs.append((a, b))
            elif b.get("Role") == "Client":
                pairs.append((b, a))
            else:
                pairs.append((a, b))
    return pairs


def _create_or_update(peers: List[Dict[str, Any]], tag_key: str, tag_value: str) -> Dict[str, str]:
    """Create all peerings + routes. Returns {label: peeringId}."""
    peering_ids: Dict[str, str] = {}

    for src, dst in _unique_pairs(peers):
        pid = _create_peering(src, dst, tag_key, tag_value)
        peering_ids[f"{src['Region']}-{dst['Region']}"] = pid

        # Routes on both sides
        _add_routes(src["Region"], src["RouteTableIds"], dst["VpcCidr"], pid)
        _add_routes(dst["Region"], dst["RouteTableIds"], src["VpcCidr"], pid)

    return peering_ids


def _delete_pair(a: Dict[str, Any], b: Dict[str, Any], tag_key: str, tag_value: str) -> None:
    """Remove both sides' routes, then the managed peering(s) between a and b.

    Deleting a peering does NOT remove routes that target it — they stay behind as
    blackholes in the region stacks' route tables. Remove them first so the tables
    are clean for a later Create.
    """
    for pcx in _find_peerings(a, b, tag=(tag_key, tag_value)):
        pid = pcx["VpcPeeringConnectionId"]
        _delete_routes(a["Region"], a["RouteTableIds"], b["VpcCidr"], pid)
        _delete_routes(b["Region"], b["RouteTableIds"], a["VpcCidr"], pid)
        try:
            _ec2(a["Region"]).delete_vpc_peering_connection(VpcPeeringConnectionId=pid)
            print(f"Deleted peering {pid} ({a['Region']}↔{b['Region']})")
        except Exception as exc:  # noqa: BLE001
            print(f"Peering delete skipped ({pid}): {exc}")


def _delete_all(peers: List[Dict[str, Any]], tag_key: str, tag_value: str) -> None:
    """Tear down managed peerings. Filter by managed tag so we don't touch anything else."""
    for a, b in _unique_pairs(peers):
        _delete_pair(a, b, tag_key, tag_value)


def _delete_removed(old_peers: List[Dict[str, Any]], new_peers: List[Dict[str, Any]],
                    tag_key: str, tag_value: str) -> None:
    """On Update, tear down pairs that are in the old Peers but not the new.

    The physical id never changes, so CloudFormation never sends a Delete for the old
    set. Without this, dropping a region left its peerings and routes behind, and the
    eventual stack Delete (which only sees the new Peers) never found them either.
    """
    keep = {frozenset((a["VpcId"], b["VpcId"])) for a, b in _unique_pairs(new_peers)}
    for a, b in _unique_pairs(old_peers):
        if frozenset((a["VpcId"], b["VpcId"])) not in keep:
            _delete_pair(a, b, tag_key, tag_value)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    print(f"Event: {json.dumps(event)}")
    response_url = event["ResponseURL"]
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})
    peers = props.get("Peers", [])
    tag_key, tag_value = _managed_tag(props)

    try:
        if request_type in ("Create", "Update"):
            peering_ids = _create_or_update(peers, tag_key, tag_value)
            if request_type == "Update":
                # Removed pairs were tagged with the OLD managed tag.
                old_props = event.get("OldResourceProperties", {})
                old_key, old_value = _managed_tag(old_props)
                _delete_removed(old_props.get("Peers", []), peers, old_key, old_value)
            _send(response_url, event, context, "SUCCESS", data=peering_ids)
        elif request_type == "Delete":
            _delete_all(peers, tag_key, tag_value)
            _send(response_url, event, context, "SUCCESS")
        else:
            _send(response_url, event, context, "FAILED",
                  reason=f"Unknown RequestType: {request_type}")
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc())
        _send(response_url, event, context, "FAILED", reason=str(exc))


# Trivial accessor for tests
def _sentinel() -> str:
    return os.environ.get("SENTINEL", "")
