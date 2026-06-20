package com.ruoyi.alarm.service;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmScheduleGroup;
import com.ruoyi.alarm.domain.AlarmScheduleStrategy;

/**
 * 排班策略 Service接口
 * 
 * @author ruoyi
 */
public interface IAlarmScheduleStrategyService
{
    /**
     * 查询排班策略列表
     * 
     * @param strategy 排班策略
     * @return 排班策略集合
     */
    public List<AlarmScheduleStrategy> selectScheduleStrategyList(AlarmScheduleStrategy strategy);

    /**
     * 查询排班策略
     * 
     * @param strategyId 排班策略主键
     * @return 排班策略
     */
    public AlarmScheduleStrategy selectScheduleStrategyById(Long strategyId);

    /**
     * 新增排班策略和排班分组
     * 
     * @param strategy 排班策略
     * @param groups 排班分组集合
     * @return 结果
     */
    public int insertScheduleStrategyWithGroups(AlarmScheduleStrategy strategy, List<AlarmScheduleGroup> groups);

    /**
     * 修改排班策略和排班分组
     * 
     * @param strategy 排班策略
     * @param groups 排班分组集合
     * @return 结果
     */
    public int updateScheduleStrategyWithGroups(AlarmScheduleStrategy strategy, List<AlarmScheduleGroup> groups);

    /**
     * 批量删除排班策略
     * 
     * @param strategyIds 需要删除的排班策略主键集合
     * @return 结果
     */
    public int deleteScheduleStrategyByIds(Long[] strategyIds);

    /**
     * 修改排班策略状态
     * 
     * @param strategy 排班策略
     * @return 结果
     */
    public int changeStatus(AlarmScheduleStrategy strategy);
}
