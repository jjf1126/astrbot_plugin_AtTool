import re
import json
import time
from typing import List, Dict, Any, Optional
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 1. 匹配合法的 ID 标签: [at:123456]
        self.valid_at_pattern = re.compile(r'\[at:(\d+)\]')
        # 2. 匹配疑似垃圾标签 (非纯数字的 at 标签)，用于除杂
        # 匹配 [at:...] 其中 ... 不是纯数字
        self.invalid_at_pattern = re.compile(r'\[at:(?!\d+\])[^\]]*\]')

    @filter.on_llm_request()
    async def inject_at_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        要求4 & 1: 在 LLM 请求前插入 Prompt，教它如何使用 @ 功能。
        使用 XML 格式展示，让 LLM 更容易理解。
        """
        # 仅在群聊环境下注入提示词
        if not event.get_group_id():
            return

        instruction = """
<function_instruction>
    <name>Mention User (@)</name>
    <description>
        You can mention (@) specific group members to get their attention.
    </description>
    <rules>
        <rule id="1">
            If you need to mention someone but do NOT know their 'user_id', call the tool `get_group_members` first.
        </rule>
        <rule id="2">
            Once you have the 'user_id', DO NOT call any tool. Instead, directly output the tag in your response text.
            Format: [at:user_id]
            Example: "Hello [at:123456], how are you?"
        </rule>
        <rule id="3">
            Do not output [at:name] or [at:unknown]. Only use the numeric user_id.
        </rule>
    </rules>
</function_instruction>
"""
        # 将提示词追加到 system_prompt 中
        req.system_prompt += instruction

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(self, event: AstrMessageEvent) -> str:
        """
        查询群成员列表。
        当你想 @ 某人，但不知道他的 user_id 时，请调用此工具。
        返回结果包含成员昵称和 user_id。
        """
        start_time = time.time()
        try:
            group_id = event.get_group_id()
            if not group_id:
                return json.dumps({"error": "Not a group chat environment."})

            if not isinstance(event, AiocqhttpMessageEvent):
                return json.dumps({"error": "Only supports OneBot(aiocqhttp) protocol."})

            members_info = await self._get_group_members_internal(event)
            if not members_info:
                return json.dumps({"error": "Failed to fetch members (permission denied or network error)."})

            # 数据清洗，只保留 LLM 需要的核心字段，减少 Token 消耗
            processed_members = []
            for member in members_info:
                uid = str(member.get("user_id", ""))
                if not uid: continue
                
                # 聚合名称，方便 LLM 模糊匹配
                names = []
                if member.get("card"): names.append(member.get("card"))
                if member.get("nickname"): names.append(member.get("nickname"))
                
                processed_members.append({
                    "user_id": uid,
                    "names": names,
                    "role": member.get("role", "member")
                })

            result_data = {
                "group_id": group_id,
                "total": len(processed_members),
                "members": processed_members
            }
            
            logger.debug(f"Fetched {len(processed_members)} members in {time.time() - start_time:.2f}s")
            return json.dumps(result_data, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error getting group members: {e}")
            return json.dumps({"error": str(e)})

    async def _get_group_members_internal(self, event: AiocqhttpMessageEvent) -> Optional[List[Dict[str, Any]]]:
        """内部函数：调用 OneBot API 获取群成员列表"""
        try:
            group_id = event.get_group_id()
            if not group_id: return None
            return await event.bot.api.call_action('get_group_member_list', group_id=group_id)
        except Exception as e:
            logger.error(f"API Call Failed: {e}")
            return None

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        要求 2 & 3: 拦截回复消息。
        1. 除杂：清除 [at:null] 等无效内容。
        2. 编码：将 [at:123] 转换为真实 At 组件。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否有标签，避免无意义循环
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                has_tag = True
                break
        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                
                # --- 要求3：除杂处理 ---
                # 将 [at:unknown], [at:张三] 等非数字 ID 的标签替换为空字符串
                text = self.invalid_at_pattern.sub("", text)

                # --- 要求2：标签编码处理 ---
                last_idx = 0
                # 查找所有合法的 [at:123]
                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()

                    # 添加标签前的纯文本
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))

                    target_id = match.group(1)
                    
                    # 插入真实 At 组件 (前后加零宽空格防止粘连)
                    new_chain.append(Plain("\u200b")) 
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain("\u200b"))

                    last_idx = end

                # 添加剩余的文本
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                new_chain.append(comp)

        result.chain = new_chain
