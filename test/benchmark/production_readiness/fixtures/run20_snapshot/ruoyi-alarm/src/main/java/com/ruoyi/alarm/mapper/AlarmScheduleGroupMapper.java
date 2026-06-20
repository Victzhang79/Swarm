package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmScheduleGroup;

/**
 * 排班分组 数据层
 *
 * @author ruoyi
 */
public interface AlarmScheduleGroupMapper
{
    /**
     * 根据排班策略ID查询排班分组列表
     *
     * @param strategyId 排班策略ID
     * @return 排班分组集合
     */
    public List<AlarmScheduleGroup> selectScheduleGroupByStrategyId(Long strategyId);

    /**
     * 批量新增排班分组
     *
     * @param groups 排班分组列表
     * @return 结果
     */
    public int batchInsertScheduleGroup(List<AlarmScheduleGroup> groups);

    /**
     * 根据排班策略ID删除排班分组
     *
     * @param strategyId 排班策略ID
     * @return 结果
     */
    public int deleteScheduleGroupByStrategyId(Long strategyId);

    /**
     * 根据排班策略ID列表删除排班分组
     *
     * @param strategyIds 排班策略ID列表
     * @return 结果
     */
    public int deleteScheduleGroupByStrategyIds(Long[] strategyIds);
}
