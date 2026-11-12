"""QQ group sends with receipts and group-message recall using AstrBot's API client."""

from urllib.parse import quote


class QQGroupDelivery:
    def __init__(self, context):
        self.context = context

    def resolve(self, origin):
        platform_id, kind, group_id = origin.split(":", 2)
        if kind != "GroupMessage":
            raise ValueError("Minecraft notices require a group session")
        platform = self.context.get_platform_inst(platform_id)
        if platform is None or platform.meta().name not in {
            "qq_official",
            "qq_official_webhook",
        }:
            raise ValueError("Official QQ adapter unavailable")
        return platform.client.api, group_id.rsplit("_", 1)[-1]

    async def send(self, origin, text):
        api, group_id = self.resolve(origin)
        result = await api.post_group_message(
            group_openid=group_id, msg_type=0, content=text
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("id"), str)
            or not result["id"]
        ):
            raise ValueError("QQ send returned no message receipt")
        return [result["id"]]

    async def recall(self, origin, message_id):
        from botpy.http import Route

        api, group_id = self.resolve(origin)
        route = Route(
            "DELETE",
            "/v2/groups/{group_openid}/messages/{message_id}",
            group_openid=quote(group_id, safe=""),
            message_id=quote(message_id, safe=""),
        )
        await api._http.request(route)
