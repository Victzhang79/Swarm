package com.ruoyi.alarm.engine.service;

import java.util.Map;

/**
 * 模板渲染服务
 * 
 * @author ruoyi
 */
public interface TemplateRenderService
{
    /**
     * 渲染模板占位符
     * 
     * @param templateContent 模板内容
     * @param variables 变量映射
     * @return 渲染后的内容
     */
    String render(String templateContent, Map<String, String> variables);
}
