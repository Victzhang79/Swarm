package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmTask;

/**
 * 预警任务 数据层
 *
 * @author ruoyi
 */
public interface AlarmTaskMapper
{
    /**
     * 查询预警任务
     *
     * @param taskId 预警任务ID
     * @return 预警任务
     */
    public AlarmTask selectAlarmTaskById(Long taskId);

    /**
     * 根据任务名称查询预警任务
     *
     * @param taskName 任务名称
     * @return 预警任务
     */
    public AlarmTask selectAlarmTaskByName(String taskName);

    /**
     * 查询预警任务列表
     *
     * @param alarmTask 预警任务
     * @return 预警任务集合
     */
    public List<AlarmTask> selectAlarmTaskList(AlarmTask alarmTask);

    /**
     * 新增预警任务
     *
     * @param alarmTask 预警任务
     * @return 结果
     */
    public int insertAlarmTask(AlarmTask alarmTask);

    /**
     * 修改预警任务
     *
     * @param alarmTask 预警任务
     * @return 结果
     */
    public int updateAlarmTask(AlarmTask alarmTask);

    /**
     * 批量删除预警任务
     *
     * @param taskIds 需要删除的预警任务ID
     * @return 结果
     */
    public int deleteAlarmTaskByIds(Long[] taskIds);

    /**
     * 删除预警任务
     *
     * @param taskId 预警任务ID
     * @return 结果
     */
    public int deleteAlarmTaskById(Long taskId);
}
