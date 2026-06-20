package com.ruoyi.alarm.engine.service.impl;

import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.engine.service.TemplateRenderService;

/**
 * 模板渲染服务实现
 * 
 * @author ruoyi
 */
@Service
public class TemplateRenderServiceImpl implements TemplateRenderService
{
    /**
     * 占位符正则表达式，匹配 #{变量名} 格式
     */
    private static final Pattern PLACEHOLDER_PATTERN = Pattern.compile("#\\{([^}]+)\\}");

    /**
     * 渲染模板占位符
     * 
     * @param templateContent 模板内容
     * @param variables 变量映射
     * @return 渲染后的内容
     */
    @Override
    public String render(String templateContent, Map<String, String> variables)
    {
        if (templateContent == null || templateContent.isEmpty())
        {
            return templateContent;
        }

        if (variables == null || variables.isEmpty())
        {
            return templateContent;
        }

        Matcher matcher = PLACEHOLDER_PATTERN.matcher(templateContent);
        StringBuffer result = new StringBuffer();

        while (matcher.find())
        {
            String variableName = matcher.group(1);
            String value = variables.get(variableName);
            if (value == null)
            {
                value = "";
            }
            matcher.appendReplacement(result, value);
        }
        matcher.appendTail(result);

        return result.toString();
    }
}
