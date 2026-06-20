package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmTemplate;

/**
 * 预警模板Mapper接口
 * 
 * @author ruoyi
 */
public interface AlarmTemplateMapper
{
    /**
     * 查询预警模板列表
     * 
     * @param template 预警模板
     * @return 预警模板集合
     */
    public List<AlarmTemplate> selectAlarmTemplateList(AlarmTemplate template);

    /**
     * 查询预警模板
     * 
     * @param templateId 模板ID
     * @return 预警模板
     */
    public AlarmTemplate selectAlarmTemplateById(Long templateId);

    /**
     * 新增预警模板
     * 
     * @param template 预警模板
     * @return 结果
     */
    public int insertAlarmTemplate(AlarmTemplate template);

    /**
     * 修改预警模板
     * 
     * @param template 预警模板
     * @return 结果
     */
    public int updateAlarmTemplate(AlarmTemplate template);

    /**
     * 删除预警模板
     * 
     * @param templateId 模板ID
     * @return 结果
     */
    public int deleteAlarmTemplateById(Long templateId);
}
