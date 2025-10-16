# -*- coding: utf-8 -*-
"""
auth_manager.py - WebUI认证管理
处理WebUI的认证和会话管理
"""

import hashlib
import time
from typing import Dict, Optional


class AuthManager:
    """
    管理WebUI的认证和会话
    """
    
    def __init__(self, password: str):
        """
        初始化认证管理器
        
        Args:
            password: WebUI访问密码
        """
        self.password = password
        self.sessions: Dict[str, float] = {}  # session_id -> expiry_time
        self.session_timeout = 3600  # 1小时
    
    def verify_password(self, password: str) -> bool:
        """
        验证密码
        
        Args:
            password: 待验证的密码
            
        Returns:
            bool: 密码是否正确
        """
        # 如果未设置密码，则直接允许访问
        if not self.password:
            return True
        
        # 简单的密码比较（实际应用中可以使用更安全的哈希方法）
        return password == self.password
    
    def generate_session(self) -> str:
        """
        生成一个新的会话ID
        
        Returns:
            str: 会话ID
        """
        session_id = hashlib.sha256(str(time.time()).encode()).hexdigest()
        expiry_time = time.time() + self.session_timeout
        self.sessions[session_id] = expiry_time
        return session_id
    
    def validate_session(self, session_id: Optional[str]) -> bool:
        """
        验证会话是否有效
        
        Args:
            session_id: 会话ID
            
        Returns:
            bool: 会话是否有效
        """
        if not session_id:
            return False
        
        if session_id not in self.sessions:
            return False
        
        current_time = time.time()
        if self.sessions[session_id] < current_time:
            # 会话已过期
            del self.sessions[session_id]
            return False
        
        # 更新会话过期时间
        self.sessions[session_id] = current_time + self.session_timeout
        return True
    
    def logout(self, session_id: str) -> None:
        """
        登出用户，删除会话
        
        Args:
            session_id: 会话ID
        """
        if session_id in self.sessions:
            del self.sessions[session_id]
    
    def cleanup_expired_sessions(self):
        """
        清理过期的会话
        """
        current_time = time.time()
        expired_sessions = [sid for sid, expiry in self.sessions.items() if expiry < current_time]
        for sid in expired_sessions:
            del self.sessions[sid]