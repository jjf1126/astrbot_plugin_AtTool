import re
from typing import List
from astrbot.api.star import Star, register, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.message.components import Plain, At, BaseMessageComponent

class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 编译正则，匹配 [mention:123456] 格式
        self.mention_pattern = re.compile(r'\[mention:(\d+)\]')

    @filter.llm_tool(name="mention_user")
    async def mention_user(self, event: AstrMessageEvent, user_id: str) -> str:
        """
        生成一个用于艾特(At)指定用户的标签。
        当你想在回复中艾特（又称提及，提醒、找、@等）某人时，请调用此工具，并将工具返回的字符串原样包含在你的最终回复中。

        Args:
            user_id (str): 用户的QQ号/ID (可以通过 get_group_members_info 工具获取)
        """
        return f"[mention:{user_id}]"

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截消息发送，将文本中的 [mention:id] 替换为真实的 At 组件。
        这是“消息构建”的核心部分，保持不变。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 快速检查是否包含标签，避免无效循环
        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[mention:" in comp.text:
                has_tag = True
                break
        
        if not has_tag:
            return

        logger.debug(f"检测到艾特标签，正在构建消息链...")
        
        new_chain: List[BaseMessageComponent] = []
        
        for comp in result.chain:
            # 只处理纯文本组件
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0
                # 使用正则查找所有标签
                for match in self.mention_pattern.finditer(text):
                    # 1. 添加标签前的文本
                    start, end = match.span()
                    if start > last_idx:
                        pre_text = text[last_idx:start]
                        if pre_text:
                            new_chain.append(Plain(pre_text))
                    
                    # 2. 添加 At 组件 (核心构建逻辑)
                    target_id = match.group(1)
                    new_chain.append(Plain("\u200b \u200b"))
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain("\u200b \u200b"))
                    last_idx = end
                
                # 3. 添加剩余的文本
                if last_idx < len(text):
                    post_text = text[last_idx:]
                    if post_text:
                        new_chain.append(Plain(post_text))
            else:
                # 图片等其他组件保持原样
                new_chain.append(comp)

        # 替换原始消息链
        result.chain = new_chain
