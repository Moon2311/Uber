"""Channel-layer group names. Clients never name groups: the server decides which
groups a connection joins, after authorization."""


def user_group(user_id) -> str:
    return f"user.{user_id}"


def driver_group(driver_id) -> str:
    return f"driver.{driver_id}"


def trip_group(trip_id) -> str:
    return f"trip.{trip_id}"
