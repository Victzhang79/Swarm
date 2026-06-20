package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmScheduleStrategy;

/**
 * 排班策略 数据层
 *
 * @author ruoyi
 */
public interface AlarmScheduleStrategyMapper
{
    /**
     * 查询排班策略列表
     *
     * @param strategy 排班策略对象
     * @return 排班策略集合
     */
    public List<AlarmScheduleStrategy> selectScheduleStrategyList(AlarmScheduleStrategy strategy);

    /**
     * 查询排班策略
     *
     * @param strategyId 排班策略ID
     * @return 排班策略对象
     */
    public AlarmScheduleStrategy selectScheduleStrategyById(Long strategyId);

    /**
     * 新增排班策略
     *
     * @param strategy 排班策略对象
     * @return 结果
     */
    public int insertScheduleStrategy(AlarmScheduleStrategy strategy);

    /**
     * 修改排班策略
     *
     * @param strategy 排班策略对象
     * @return 结果
     */
    public int updateScheduleStrategy(AlarmScheduleStrategy strategy);

    /**
     * 删除排班策略
     *
     * @param strategyId 排班策略ID
     * @return 结果
     */
    public int deleteScheduleStrategyById(Long strategyId);

    /**
     * 批量删除排班策略
     *
     * @param strategyIds 需要删除的排班策略ID
     * @return 结果
     */
    public int deleteScheduleStrategyByIds(Long[] strategyIds);

    /**
     * 级联删除排班策略关联的分组
     *
     * @param strategyId 排班策略ID
     * @return 结果
     */
    public int deleteGroupByStrategyId(Long strategyId);
}
