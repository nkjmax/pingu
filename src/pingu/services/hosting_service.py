"""
Mix requests only -- oPUG and fresh pug don't go through here. The flow:

    submit_request()  -> host_requests row created
    attach_thread()   -> thread_id saved once the thread under the
                          mix-requests channel is created
    attach_roster()   -> roster saved once the requester pings their team
                          in-thread (see cogs/host_request.py's listener)
    approve_request()  -> creates the actual match + assigns captain
    deny_request()      -> marks denied, no match created

Unresolved requests (nobody ever accepted/denied) are a known gap --
their thread just sits open indefinitely. Not handled yet.
"""

import time
import pingu.db.host_requests as requests_db
import pingu.db.matches as matches_db


class AlreadyResolved(Exception):
    pass


async def submit_request(requester_id, team_name, division, map_name, server, timestamp, notes=None) -> int:
    return await requests_db.create_request(
        requester_id, team_name, division, map_name, server, timestamp, notes
    )


async def attach_thread(request_id, thread_id):
    await requests_db.set_thread(request_id, thread_id)


async def approve_request(request_id: int, hoster_id: int) -> int:
    """
    Creates the match and makes the requester captain. Returns the new
    match_id. Raises AlreadyResolved if someone else already actioned this
    request (e.g. two hosters clicking accept at once).
    """
    request = await requests_db.get_request(request_id)
    if not request or request["status"] != "pending":
        raise AlreadyResolved(
            f"host_request #{request_id} is already {request['status'] if request else 'missing'}"
        )

    await requests_db.set_status(request_id, "approved", hoster_id=hoster_id)

    # created_by is the approving hoster (supervisor); captain_id is the
    # requester -- deliberately different people. Uses the actual scheduled
    # time collected on the request form (same field the hoster flow has).
    match_id = await matches_db.create_match(
        type_="mix",
        timestamp=request["timestamp"] or int(time.time()),
        created_by=hoster_id,
        created_by_name=str(hoster_id),
        captain_id=request["requester_id"],
        team_name=request["team_name"],
        division=request["division"],
        map_name=request["map_name"],
        server=request["server"],
        host_request_id=request_id,
    )
    if request["roster"]:
        await matches_db.update_match_fields(match_id, host_roster=request["roster"])
    return match_id


async def deny_request(request_id: int, hoster_id: int):
    request = await requests_db.get_request(request_id)
    if not request or request["status"] != "pending":
        raise AlreadyResolved(
            f"host_request #{request_id} is already {request['status'] if request else 'missing'}"
        )
    await requests_db.set_status(request_id, "denied", hoster_id=hoster_id)