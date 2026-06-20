package com.ruoyi.alarm.service.impl;

import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.mapper.AlarmTemplateMapper;
import com.ruoyi.alarm.domain.AlarmTemplate;
import com.ruoyi.alarm.service.IAlarmTemplateService;

/**
 * 预警模板Service业务层处理
 * 
 * @author ruoyi
 */
@Service
public class AlarmTemplateServiceImpl implements IAlarmTemplateService
{
    @Autowired
    private AlarmTemplateMapper alarmTemplateMapper;

    /**
     * 查询预警模板列表
     * 
     * @param template 预警模板
     * @return 预警模板集合
     */
    @Override
    public List<AlarmTemplate> selectAlarmTemplateList(AlarmTemplate template)
    {
        return alarmTemplateMapper.selectAlarmTemplateList(template);
    }

    /**
     * 查询预警模板
     * 
     * @param templateId 预警模板ID
     * @return 预警模板
     */
    @Override
    public AlarmTemplate selectAlarmTemplateById(Long templateId)
    {
        return alarmTemplateMapper.selectAlarmTemplateById(templateId);
    }

    /**
     * 新增预警模板
     * 
     * @param template 预警模板
     * @return 结果
     */
    @Override
    public int insertAlarmTemplate(AlarmTemplate template)
    {
        if (template.getStatus() == null || template.getStatus().isEmpty())
        {
            template.setStatus("0");
        }
        return alarmTemplateMapper.insertAlarmTemplate(template);
    }

    /**
     * 修改预警模板
     * 
     * @param template 预警模板
     * @return 结果
     */
    @Override
    public int updateAlarmTemplate(AlarmTemplate template)
    {
        return alarmTemplateMapper.updateAlarmTemplate(template);
    }

    /**
     * 删除预警模板
     * 
     * @param templateId 预警模板ID
     * @return 结果
     */
    @Override
    public int deleteAlarmTemplateById(Long templateId)
    {
        return alarmTemplateMapper.deleteAlarmTemplateById(templateId);
    }

    /**
     * 批量删除预警模板
     * 
     * @param templateIds 需要删除的ID
     * @return 结果
     */
    @Override
    public int deleteAlarmTemplateByIds(Long[] templateIds)
    {
        int count = 0;
        for (Long templateId : templateIds)
        {
            count += deleteAlarmTemplateById(templateId);
        }
        return count;
    }

    /**
     * 预览模板占位符替换效果
     * 
     * @param templateId 模板ID
     * @return 预览内容
     */
    @Override
    public String previewTemplate(Long templateId)
    {
        AlarmTemplate template = selectAlarmTemplateById(templateId);
        if (template == null || template.getTemplateContent() == null)
        {
            return "";
        }

        String content = template.getTemplateContent();
        Pattern pattern = Pattern.compile("#\\{(\\w+)}");
        Matcher matcher = pattern.matcher(content);

        Map<String, String> sampleValues = new HashMap<>();
        sampleValues.put("taskName", "示例任务名称");
        sampleValues.put("alarmLevel", "P1");
        sampleValues.put("alarmType", "服务异常");
        sampleValues.put("idempotentValue", "示例幂等值");
        sampleValues.put("notifyUserIds", "示例用户ID");
        sampleValues.put("content", "示例内容");
        sampleValues.put("title", "示例标题");
        sampleValues.put("channelType", "slack");
        sampleValues.put("robotName", "示例机器人");
        sampleValues.put("userName", "示例用户");
        sampleValues.put("createTime", "2024-01-01 12:00:00");

        StringBuffer sb = new StringBuffer();
        while (matcher.find())
        {
            String variable = matcher.group(1);
            String value = sampleValues.get(variable);
            if (value == null)
            {
                value = "示例_" + variable;
            }
            matcher.appendReplacement(sb, value);
        }
        matcher.appendTail(sb);

        return sb.toString();
    }
}
