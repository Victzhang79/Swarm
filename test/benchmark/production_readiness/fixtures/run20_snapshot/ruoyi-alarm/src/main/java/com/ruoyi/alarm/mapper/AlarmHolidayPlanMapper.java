package com.ruoyi.alarm.mapper;

import java.util.Date;
import java.util.List;
import org.apache.ibatis.annotations.Param;
import com.ruoyi.alarm.domain.AlarmHolidayPlan;

/**
 * 节假日计划 Mapper接口
 *
 * @author ruoyi
 */
public interface AlarmHolidayPlanMapper
{
    /**
     * 查询节假日计划列表
     *
     * @param plan 节假日计划对象
     * @return 节假日计划集合
     */
    public List<AlarmHolidayPlan> selectHolidayPlanList(AlarmHolidayPlan plan);

    /**
     * 根据ID查询节假日计划
     *
     * @param planId 节假日计划ID
     * @return 节假日计划对象
     */
    public AlarmHolidayPlan selectHolidayPlanById(Long planId);

    /**
     * 新增节假日计划
     *
     * @param plan 节假日计划对象
     * @return 结果
     */
    public int insertHolidayPlan(AlarmHolidayPlan plan);

    /**
     * 修改节假日计划
     *
     * @param plan 节假日计划对象
     * @return 结果
     */
    public int updateHolidayPlan(AlarmHolidayPlan plan);

    /**
     * 删除节假日计划
     *
     * @param planId 节假日计划ID
     * @return 结果
     */
    public int deleteHolidayPlanById(Long planId);

    /**
     * 批量删除节假日计划
     *
     * @param planIds 需要删除的ID数组
     * @return 结果
     */
    public int deleteHolidayPlanByIds(Long[] planIds);

    /**
     * 按日期范围查询节假日计划
     *
     * @param startDate 开始日期
     * @param endDate 结束日期
     * @return 节假日计划集合
     */
    public List<AlarmHolidayPlan> selectHolidayPlanByDateRange(@Param("startDate") Date startDate, @Param("endDate") Date endDate);

    /**
     * 判断指定日期是否为节假日
     *
     * @param date 日期
     * @param strategyId 排班策略ID
     * @return 节假日计划对象，非null表示是节假日
     */
    public AlarmHolidayPlan selectHolidayByDate(@Param("date") Date date, @Param("strategyId") Long strategyId);
}
